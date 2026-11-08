import os
import time
import logging
import json
import re
from typing import List, Optional, Dict
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPICallError

logger = logging.getLogger(__name__)

# ── Verified Production Models (Speed & Reliability Order) ─────────────────
# Free-tier quotas are tracked PER MODEL in Google AI Studio:
# • gemini-2.5-flash       — 15 RPM, high quality hybrid reasoning
# • gemini-2.0-flash       — 15 RPM, ultra-fast multimodal
# • gemini-2.0-flash-lite  — 15 RPM, high throughput, low latency
# • gemini-1.5-flash       — 15 RPM, stable standard fallback
# • gemini-1.5-flash-8b    — 15 RPM, lightweight & fast
# • gemini-1.5-pro         — 2 RPM, heavy fallback
DEFAULT_MODEL_FALLBACK_LIST = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
]

# Per-request timeout (seconds). Prevents any single model from stalling.
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "25"))

# Cache of dynamically discovered models per key (to avoid re-querying on every request)
CACHED_AVAILABLE_MODELS: Dict[str, List[str]] = {}

# Cooldown registry for keys that encounter 429 ResourceExhausted {api_key: cooldown_until_timestamp}
_KEY_COOLDOWNS: Dict[str, float] = {}
_LAST_KEY_INDEX = 0


def get_gemini_api_keys() -> List[str]:
    """Retrieve all configured Gemini API keys from environment variables.
    
    Supports:
    - GEMINI_API_KEYS: Comma/space/semicolon-separated list of keys
    - GEMINI_API_KEY_1, GEMINI_API_KEY_2, ... GEMINI_API_KEY_10
    - GEMINI_API_KEY
    - GOOGLE_API_KEY
    
    Returns de-duplicated list of non-empty keys, preserving order.
    """
    keys: List[str] = []

    # 1. Check GEMINI_API_KEYS (comma/semicolon/newline separated)
    raw_multi = os.getenv("GEMINI_API_KEYS", "").strip()
    if raw_multi:
        for k in re.split(r"[,;\n\s]+", raw_multi):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)

    # 2. Check standard primary singular env vars
    for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        k = os.getenv(env_var, "").strip()
        if k and k not in keys:
            keys.append(k)

    # 3. Check numbered keys GEMINI_API_KEY_1 .. GEMINI_API_KEY_10
    for i in range(1, 11):
        k = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if k and k not in keys:
            keys.append(k)

    return keys


def get_active_gemini_keys() -> List[str]:
    """Return keys that are not currently cooling down from 429 quota exhaustion.
    If all configured keys are cooling down, returns all keys (preferring the one with earliest expiry).
    """
    all_keys = get_gemini_api_keys()
    if not all_keys:
        return []

    now = time.time()
    active = [k for k in all_keys if _KEY_COOLDOWNS.get(k, 0) <= now]
    if active:
        return active

    # If all keys are in cooldown, sort by which one finishes cooldown first
    logger.warning("All Gemini API keys are currently marked in cooldown. Re-trying with earliest key.")
    return sorted(all_keys, key=lambda k: _KEY_COOLDOWNS.get(k, 0))


def record_key_quota_exhaustion(api_key: str, cooldown_seconds: int = 60):
    """Mark an API key as exhausted for cooldown_seconds to prevent repeated 429 failures."""
    key_hint = f"...{api_key[-4:]}" if len(api_key) > 6 else "key"
    _KEY_COOLDOWNS[api_key] = time.time() + cooldown_seconds
    logger.warning(f"⏳ Marked Gemini key {key_hint} on cooldown for {cooldown_seconds}s due to quota exhaustion.")


def discover_available_models(api_key: str) -> List[str]:
    """Query Google AI Studio for the exact models supported by this API key."""
    if api_key in CACHED_AVAILABLE_MODELS:
        return CACHED_AVAILABLE_MODELS[api_key]

    try:
        genai.configure(api_key=api_key)
        discovered = []
        for m in genai.list_models():
            methods = getattr(m, "supported_generation_methods", []) or []
            if "generateContent" in methods:
                name = m.name.replace("models/", "")
                # Only include chat/text/vision models, ignore embedding/aqa models
                if "embedding" not in name and "aqa" not in name:
                    discovered.append(name)

        if discovered:
            speed_rank = [
                "gemini-2.5-flash",
                "gemini-2.0-flash",
                "gemini-2.0-flash-lite",
                "gemini-flash-latest",
                "gemini-1.5-flash",
                "gemini-1.5-flash-8b",
                "gemini-1.5-pro",
            ]
            ranked = [m for m in speed_rank if m in discovered]
            others = [m for m in discovered if m not in ranked]
            model_list = ranked + others
            CACHED_AVAILABLE_MODELS[api_key] = model_list
            logger.info(f"🌟 Discovered {len(model_list)} models for key. Priority order: {ranked[:3]}")
            return model_list
    except Exception as e:
        logger.warning(f"Could not query list_models dynamically: {e}")

    # Fallback to static list if dynamic discovery is unavailable
    CACHED_AVAILABLE_MODELS[api_key] = DEFAULT_MODEL_FALLBACK_LIST
    return DEFAULT_MODEL_FALLBACK_LIST


def get_model_pool(api_key: Optional[str] = None) -> List[str]:
    """Return model list, prioritizing any user-specified model from GEMINI_MODEL."""
    preferred = os.getenv("GEMINI_MODEL", "").strip()
    pool = []
    if preferred:
        pool.append(preferred)

    base_list = discover_available_models(api_key) if api_key else DEFAULT_MODEL_FALLBACK_LIST
    for m in base_list:
        if m not in pool:
            pool.append(m)
    return pool


def generate_content_with_retry(
    contents,
    generation_config: Optional[dict] = None,
    max_retries_per_model: int = 1,
    timeout: Optional[int] = None,
) -> str:
    """Generate content using Gemini with automatic multi-key rotation and multi-model fallback.
    
    If one Gemini API key hits 429 quota exhaustion, it marks that key in cooldown and
    seamlessly rotates to the next available Gemini key in the pool without user impact.
    """
    global _LAST_KEY_INDEX

    active_keys = get_active_gemini_keys()
    if not active_keys:
        raise ValueError(
            "Missing GEMINI_API_KEY (or GEMINI_API_KEYS) in environment variables. "
            "Please configure at least one valid Gemini API key."
        )

    # Rotate starting key for load balancing across keys
    total_keys = len(active_keys)
    start_index = _LAST_KEY_INDEX % total_keys
    ordered_keys = active_keys[start_index:] + active_keys[:start_index]
    _LAST_KEY_INDEX = (start_index + 1) % total_keys

    req_timeout = timeout or REQUEST_TIMEOUT
    last_error = None
    meaningful_error = None

    for key_idx, api_key in enumerate(ordered_keys):
        key_hint = f"...{api_key[-4:]}" if len(api_key) > 6 else f"#{key_idx+1}"
        genai.configure(api_key=api_key)
        models = get_model_pool(api_key)

        key_hit_quota = False

        for model_name in models:
            for attempt in range(max_retries_per_model + 1):
                try:
                    logger.info(f"⚡ Gemini [Key {key_hint}] → {model_name} (attempt {attempt + 1}, timeout {req_timeout}s)")
                    model = genai.GenerativeModel(model_name)
                    kwargs = {"request_options": {"timeout": req_timeout}}
                    cfg = dict(generation_config or {})
                    if "thinking_config" not in cfg:
                        cfg["thinking_config"] = {"thinking_budget": 0}
                    kwargs["generation_config"] = cfg

                    try:
                        response = model.generate_content(contents, **kwargs)
                    except Exception as ex:
                        if "thinking" in str(ex).lower():
                            kwargs["generation_config"] = generation_config or {}
                            response = model.generate_content(contents, **kwargs)
                        else:
                            raise ex

                    if response and response.text:
                        logger.info(f"✅ Success with Gemini [Key {key_hint}] → {model_name}")
                        return response.text.strip()
                    else:
                        raise RuntimeError("Gemini returned an empty response.")

                except ResourceExhausted as e:
                    logger.warning(f"429 quota hit on Gemini [Key {key_hint}] model '{model_name}'")
                    last_error = e
                    meaningful_error = e
                    # If quota exhausted on this model, check next model; if repeated, rotate key
                    break

                except GoogleAPICallError as e:
                    err_str = str(e).lower()
                    if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                        logger.warning(f"Rate limited on Gemini [Key {key_hint}] model '{model_name}'")
                        last_error = e
                        meaningful_error = e
                        break
                    if "invalid api key" in err_str or "api_key_invalid" in err_str:
                        logger.error(f"Invalid API key [Key {key_hint}]. Skipping this key completely.")
                        key_hit_quota = True
                        break
                    logger.error(f"API error on Gemini [Key {key_hint}] model '{model_name}': {e}")
                    last_error = e
                    if "404" in err_str or "not found" in err_str:
                        break
                    meaningful_error = e
                    time.sleep(0.3)

                except Exception as e:
                    err_str = str(e).lower()
                    if "429" in err_str or "quota" in err_str:
                        logger.warning(f"Quota error on Gemini [Key {key_hint}] model '{model_name}'")
                        last_error = e
                        meaningful_error = e
                        break
                    if "deadline" in err_str or "timeout" in err_str or "timed out" in err_str:
                        logger.warning(f"Timeout on Gemini [Key {key_hint}] model '{model_name}' after {req_timeout}s")
                        last_error = e
                        meaningful_error = e
                        break
                    logger.error(f"Unexpected error with Gemini [Key {key_hint}] model '{model_name}': {e}")
                    last_error = e
                    meaningful_error = e
                    time.sleep(0.3)

            # If quota was hit across models on this key, switch key
            if last_error and any(q in str(last_error).lower() for q in ("429", "quota", "resource_exhausted")):
                key_hit_quota = True

        if key_hit_quota and len(ordered_keys) > 1:
            record_key_quota_exhaustion(api_key, cooldown_seconds=60)
            logger.info(f"🔄 Rotating to next Gemini API key in pool ({len(ordered_keys)-key_idx-1} remaining)...")
            continue

    err_to_report = meaningful_error or last_error
    err_message = (
        f"AI rate limit reached across all {len(ordered_keys)} configured Gemini API key(s) and models. "
        "Please wait 30–60 seconds before retrying, or configure GROQ_API_KEY for instant multi-provider fallback. "
        f"(Underlying error: {err_to_report})"
    )
    raise RuntimeError(err_message)
