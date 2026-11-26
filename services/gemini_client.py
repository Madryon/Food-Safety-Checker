import os
import io
import time
import base64
import logging
import re
from typing import List, Optional, Dict, Any, Tuple
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger(__name__)

# Thread-safe, connection-pooled HTTP session for Gemini REST calls
_GEMINI_SESSION = requests.Session()
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# ── Verified Production Models (Fast Multimodal Flash Order) ───────────────
# Keep ONLY fast multimodal Flash models in the fallback pool so failures never
# cascade through 40+ slow/incompatible models (like Pro, TTS, Robotics, Gemma).
DEFAULT_MODEL_FALLBACK_LIST = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-flash-latest",
    "gemini-1.5-flash",
]

# Non-multimodal / slow / specialized model substrings to exclude from hot path
_EXCLUDED_MODEL_SUBSTRINGS = (
    "tts",
    "audio",
    "live",
    "image",
    "imagen",
    "veo",
    "embedding",
    "aqa",
    "robotics",
    "computer-use",
    "deep-research",
    "thinking",
    "gemma",
    "nano-banana",
    "pro",
)

# Per-request timeout (seconds). Prevents any single model from stalling.
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "15"))

# Cache of dynamically discovered fast models per key
CACHED_AVAILABLE_MODELS: Dict[str, List[str]] = {}

# Cooldown registry for keys {api_key: cooldown_until_timestamp}
_KEY_COOLDOWNS: Dict[str, float] = {}
# Last error observed per key (for transparent API diagnostics without leaking keys)
_KEY_LAST_ERRORS: Dict[str, str] = {}
_LAST_KEY_INDEX = 0


def _mask_key(api_key: str) -> str:
    return f"...{api_key[-4:]}" if len(api_key) > 6 else "key"


def get_gemini_api_keys() -> List[str]:
    """Retrieve all configured Gemini API keys from environment variables.

    Supports:
    - GEMINI_API_KEYS: Comma/space/semicolon-separated list of keys
    - GEMINI_API_KEY_1 .. GEMINI_API_KEY_10
    - GEMINI_API_KEY
    - GOOGLE_API_KEY

    Returns de-duplicated list of non-empty keys, preserving order.
    """
    keys: List[str] = []

    raw_multi = os.getenv("GEMINI_API_KEYS", "").strip()
    if raw_multi:
        for k in re.split(r"[,;\n\s]+", raw_multi):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)

    for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        k = os.getenv(env_var, "").strip()
        if k and k not in keys:
            keys.append(k)

    for i in range(1, 11):
        k = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if k and k not in keys:
            keys.append(k)

    return keys


def get_active_gemini_keys() -> List[str]:
    """Return keys that are not currently cooling down from quota/auth failures.

    If all configured keys are cooling down, retries temporary cooldowns (< 600s)
    first, or still returns retryable keys if no active key exists.
    """
    all_keys = get_gemini_api_keys()
    if not all_keys:
        return []

    now = time.time()
    active = [k for k in all_keys if _KEY_COOLDOWNS.get(k, 0) <= now]
    if active:
        return active

    retryable = [k for k in all_keys if (_KEY_COOLDOWNS.get(k, 0) - now) < 600]
    if retryable:
        logger.warning("All Gemini API keys are in cooldown. Retrying with earliest expiry key.")
        return sorted(retryable, key=lambda k: _KEY_COOLDOWNS.get(k, 0))
    return []


def record_key_quota_exhaustion(api_key: str, cooldown_seconds: int = 60):
    """Mark an API key as exhausted for cooldown_seconds to prevent repeated 429 failures."""
    key_hint = _mask_key(api_key)
    _KEY_COOLDOWNS[api_key] = time.time() + cooldown_seconds
    logger.warning(f"Marked Gemini key {key_hint} on cooldown for {cooldown_seconds}s due to quota exhaustion.")


def get_key_diagnostics() -> List[Dict[str, str]]:
    """Return masked last-error diagnostics for configured Gemini keys."""
    out: List[Dict[str, str]] = []
    now = time.time()
    for idx, k in enumerate(get_gemini_api_keys(), 1):
        cd = _KEY_COOLDOWNS.get(k, 0) - now
        out.append({
            "key_index": idx,
            "preview": f"{k[:6]}...{k[-4:]}" if len(k) > 10 else f"key_{idx}",
            "cooldown_remaining_s": max(0, int(cd)),
            "last_error": _KEY_LAST_ERRORS.get(k, ""),
        })
    return out


def discover_available_models(api_key: str) -> List[str]:
    """Query Google AI Studio REST API for supported fast Flash models for this key."""
    if api_key in CACHED_AVAILABLE_MODELS:
        return CACHED_AVAILABLE_MODELS[api_key]

    try:
        resp = _GEMINI_SESSION.get(
            f"{_GEMINI_API_BASE}/models?pageSize=100",
            headers={"x-goog-api-key": api_key},
            timeout=(3.0, 4.0),
        )
        if resp.status_code == 200:
            discovered = []
            for m in resp.json().get("models", []):
                methods = m.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                name = (m.get("name") or "").replace("models/", "")
                low = name.lower()
                if "flash" not in low:
                    continue
                if any(bad in low for bad in _EXCLUDED_MODEL_SUBSTRINGS):
                    continue
                discovered.append(name)

            if discovered:
                speed_rank = [
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-2.0-flash-lite",
                    "gemini-flash-latest",
                    "gemini-1.5-flash",
                ]
                ranked = [m for m in speed_rank if m in discovered]
                extra_flash = [m for m in discovered if m not in ranked]
                # Cap pool to at most 4 fast Flash models to prevent long waterfall delays
                model_list = (ranked + extra_flash)[:4]
                CACHED_AVAILABLE_MODELS[api_key] = model_list
                logger.info(f"Discovered {len(model_list)} fast Flash models for key {_mask_key(api_key)}: {model_list}")
                return model_list
        else:
            err_msg = _extract_rest_error(resp)
            _KEY_LAST_ERRORS[api_key] = err_msg
            logger.warning(f"Could not query list_models for key {_mask_key(api_key)}: {err_msg}")
    except Exception as e:
        logger.warning(f"Could not query list_models dynamically: {e}")

    CACHED_AVAILABLE_MODELS[api_key] = DEFAULT_MODEL_FALLBACK_LIST[:4]
    return CACHED_AVAILABLE_MODELS[api_key]


def get_model_pool(api_key: Optional[str] = None) -> List[str]:
    """Return curated fast model list, prioritizing any user-specified GEMINI_MODEL."""
    preferred = os.getenv("GEMINI_MODEL", "").strip()
    pool = []
    if preferred:
        pool.append(preferred)

    base_list = discover_available_models(api_key) if api_key else DEFAULT_MODEL_FALLBACK_LIST[:4]
    for m in base_list:
        if m not in pool:
            pool.append(m)
    return pool[:4]


def _extract_rest_error(resp: requests.Response) -> str:
    """Extract clean, human-readable error string from a Gemini HTTP error response."""
    try:
        err = resp.json().get("error", {})
        msg = (err.get("message") or "").strip()
        status_str = (err.get("status") or "").strip()
        details = err.get("details") or []
        reason = next((d.get("reason", "") for d in details if isinstance(d, dict) and d.get("reason")), "")
        parts = [f"HTTP {resp.status_code}"]
        if status_str:
            parts.append(status_str)
        out = " ".join(parts)
        if msg:
            out += f": {msg}"
        if reason:
            out += f" [{reason}]"
        return out[:280]
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"


def _build_gemini_parts(contents: Any) -> List[Dict[str, Any]]:
    """Convert string, PIL Image, inline_data dict, or list of items into Gemini REST parts."""
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    parts: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            parts.append({"text": item})
        elif isinstance(item, dict):
            if "inline_data" in item or "text" in item:
                parts.append(item)
            elif "mime_type" in item and "data" in item:
                raw_data = item["data"]
                b64 = base64.b64encode(raw_data).decode("utf-8") if isinstance(raw_data, (bytes, bytearray)) else raw_data
                parts.append({"inline_data": {"mime_type": item["mime_type"], "data": b64}})
        elif Image is not None and isinstance(item, Image.Image):
            img = item.convert("RGB") if item.mode != "RGB" else item
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
        else:
            parts.append({"text": str(item)})
    return parts


def _build_rest_gen_config(
    model_name: str,
    generation_config: Optional[dict],
    json_mode: bool,
) -> Dict[str, Any]:
    """Build camelCase REST generationConfig dictionary."""
    cfg: Dict[str, Any] = {}
    if generation_config:
        mapping = {
            "temperature": "temperature",
            "top_p": "topP",
            "top_k": "topK",
            "max_output_tokens": "maxOutputTokens",
            "response_mime_type": "responseMimeType",
            "candidate_count": "candidateCount",
        }
        for k, v in generation_config.items():
            if k in mapping:
                cfg[mapping[k]] = v
            elif k in ("temperature", "topP", "topK", "maxOutputTokens", "responseMimeType", "thinkingConfig"):
                cfg[k] = v

    if json_mode and "responseMimeType" not in cfg:
        cfg["responseMimeType"] = "application/json"

    # Disable default "thinking" budget on Gemini 2.5 Flash so OCR/classification takes ~1s instead of 10s+
    if ("2.5" in model_name or "3." in model_name) and "thinkingConfig" not in cfg:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}

    return cfg


def generate_content_with_retry(
    contents,
    generation_config: Optional[dict] = None,
    max_retries_per_model: int = 1,
    timeout: Optional[int] = None,
    total_timeout: Optional[float] = None,
    json_mode: bool = False,
) -> str:
    """Generate content via thread-safe Gemini REST API with multi-key rotation and capped fast fallback.

    Enforces a strict total wall-clock budget so failures fail fast with clear API error messages
    instead of stalling 60+ seconds across dozens of models.
    """
    global _LAST_KEY_INDEX

    configured_keys = get_gemini_api_keys()
    if not configured_keys:
        raise ValueError(
            "Missing GEMINI_API_KEY (or GEMINI_API_KEYS) in environment variables. "
            "Please configure at least one valid Gemini API key."
        )

    active_keys = get_active_gemini_keys()
    if not active_keys:
        # All keys are temporarily cooled down (e.g. invalid 401/403). Probe earliest key once cleanly
        # AND surface the exact recorded API error instead of falsely claiming key is missing!
        last_errs = [f"{_mask_key(k)}: {_KEY_LAST_ERRORS[k]}" for k in configured_keys if k in _KEY_LAST_ERRORS]
        if last_errs:
            raise RuntimeError(f"Gemini API error on all {len(configured_keys)} key(s): {'; '.join(last_errs)}")
        raise RuntimeError(
            f"All {len(configured_keys)} Gemini API key(s) are currently unavailable or in cooldown."
        )

    total_keys = len(active_keys)
    start_index = _LAST_KEY_INDEX % total_keys
    ordered_keys = active_keys[start_index:] + active_keys[:start_index]
    _LAST_KEY_INDEX = (start_index + 1) % total_keys

    req_timeout = float(timeout or REQUEST_TIMEOUT)
    budget = float(total_timeout) if total_timeout is not None else max(req_timeout * 1.8, 12.0)
    deadline = time.monotonic() + budget

    parts = _build_gemini_parts(contents)
    last_error = ""
    per_key_errors: List[str] = []

    for key_idx, api_key in enumerate(ordered_keys):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break

        key_hint = _mask_key(api_key)
        models = get_model_pool(api_key)
        key_invalid = False
        quota_hits_on_key = 0

        for model_name in models:
            remaining = deadline - time.monotonic()
            if key_invalid or quota_hits_on_key >= 2 or remaining <= 0.5:
                break

            call_timeout = min(req_timeout, max(3.0, remaining))
            url = f"{_GEMINI_API_BASE}/models/{model_name}:generateContent"
            headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
            gen_cfg = _build_rest_gen_config(model_name, generation_config, json_mode=json_mode)

            for attempt in range(max_retries_per_model + 1):
                if (deadline - time.monotonic()) <= 0.5:
                    break
                payload = {
                    "contents": [{"parts": parts}],
                }
                if gen_cfg:
                    payload["generationConfig"] = gen_cfg

                try:
                    t_req = time.monotonic()
                    logger.info(f"Gemini [Key {key_hint}] -> {model_name} (timeout {call_timeout:.1f}s)")
                    resp = _GEMINI_SESSION.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=(min(3.5, call_timeout), call_timeout),
                    )
                    elapsed_ms = int((time.monotonic() - t_req) * 1000)

                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates") or []
                        c_parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
                        text = "".join(p.get("text", "") for p in c_parts if isinstance(p, dict)).strip()
                        if text:
                            _KEY_LAST_ERRORS.pop(api_key, None)
                            logger.info(f"Gemini [Key {key_hint}] -> {model_name} succeeded in {elapsed_ms}ms")
                            return text
                        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
                        last_error = f"Empty response (blockReason={block_reason})"
                        _KEY_LAST_ERRORS[api_key] = last_error
                        break

                    err_msg = _extract_rest_error(resp)
                    err_low = err_msg.lower()
                    last_error = err_msg
                    _KEY_LAST_ERRORS[api_key] = err_msg

                    # If this model rejects thinkingConfig, drop it and retry immediately
                    if resp.status_code == 400 and "thinking" in err_low and "thinkingConfig" in gen_cfg:
                        gen_cfg.pop("thinkingConfig", None)
                        continue

                    # Auth / key / API restriction errors -> fail key immediately (no retry across 4 models)
                    if resp.status_code in (401, 403) or any(
                        tok in err_low
                        for tok in (
                            "api key not valid",
                            "api_key_invalid",
                            "access_token_type_unsupported",
                            "permission_denied",
                            "unauthenticated",
                        )
                    ):
                        logger.error(f"Gemini auth error [Key {key_hint}]: {err_msg}")
                        _KEY_COOLDOWNS[api_key] = time.time() + 300
                        per_key_errors.append(f"Key {key_hint}: {err_msg}")
                        key_invalid = True
                        break

                    # Quota / rate limit -> break attempt loop cleanly
                    if resp.status_code == 429 or "resource_exhausted" in err_low or "quota" in err_low:
                        logger.warning(f"Gemini 429 quota [Key {key_hint}] model '{model_name}': {err_msg}")
                        quota_hits_on_key += 1
                        if quota_hits_on_key >= 2:
                            record_key_quota_exhaustion(api_key, cooldown_seconds=60)
                            per_key_errors.append(f"Key {key_hint}: {err_msg}")
                        break

                    # 404 or unsupported model feature -> skip directly to next model
                    if resp.status_code in (400, 404):
                        logger.warning(f"Gemini {resp.status_code} on [Key {key_hint}] model '{model_name}': {err_msg}")
                        break

                    logger.warning(f"Gemini error [Key {key_hint}] model '{model_name}' in {elapsed_ms}ms: {err_msg}")

                except requests.exceptions.Timeout:
                    last_error = f"Timed out after {call_timeout:.1f}s on {model_name}"
                    _KEY_LAST_ERRORS[api_key] = last_error
                    logger.warning(f"Gemini timeout [Key {key_hint}] on '{model_name}' after {call_timeout:.1f}s")
                    break
                except Exception as e:
                    last_error = str(e)[:220]
                    _KEY_LAST_ERRORS[api_key] = last_error
                    logger.warning(f"Gemini unexpected error [Key {key_hint}] model '{model_name}': {last_error}")
                    break

        if not key_invalid and quota_hits_on_key == 1:
            record_key_quota_exhaustion(api_key, cooldown_seconds=45)
        if not per_key_errors or not any(key_hint in e for e in per_key_errors):
            if last_error:
                per_key_errors.append(f"Key {key_hint}: {last_error}")

    summary = "; ".join(per_key_errors) if per_key_errors else (last_error or "Request timed out")
    raise RuntimeError(f"Gemini failed across {len(ordered_keys)} key(s): {summary}")
