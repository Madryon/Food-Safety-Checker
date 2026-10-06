import os
import time
import logging
import json
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPICallError

logger = logging.getLogger(__name__)

# ── Verified Production Models (Speed & Reliability Order) ─────────────────
# Free-tier quotas are tracked PER MODEL in Google AI Studio:
# • gemini-2.0-flash       — 15 RPM, ultra-fast multimodal
# • gemini-2.5-flash       — 15 RPM, high quality hybrid reasoning
# • gemini-2.5-flash-lite  — 15 RPM, high throughput, low latency
# • gemini-1.5-flash       — 15 RPM, stable standard fallback
# • gemini-1.5-flash-8b    — 15 RPM, lightweight & fast
# • gemini-1.5-pro         — 2 RPM, heavy fallback
DEFAULT_MODEL_FALLBACK_LIST = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
]

# Per-request timeout (seconds). Prevents any single model from stalling.
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "25"))

CACHED_AVAILABLE_MODELS: list[str] | None = None


def discover_available_models(api_key: str) -> list[str]:
    """Query Google AI Studio for the exact models supported by this API key."""
    global CACHED_AVAILABLE_MODELS
    if CACHED_AVAILABLE_MODELS:
        return CACHED_AVAILABLE_MODELS

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
            # Reorder with fastest models first
            speed_rank = [
                "gemini-2.0-flash",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
                "gemini-1.5-flash",
                "gemini-1.5-flash-8b",
                "gemini-1.5-pro",
            ]
            ranked = [m for m in speed_rank if m in discovered]
            others = [m for m in discovered if m not in ranked]
            CACHED_AVAILABLE_MODELS = ranked + others
            logger.info(f"🌟 Discovered {len(CACHED_AVAILABLE_MODELS)} valid models from API: {CACHED_AVAILABLE_MODELS}")
            return CACHED_AVAILABLE_MODELS
    except Exception as e:
        logger.warning(f"Could not query list_models dynamically: {e}")

    # Fallback to static list if dynamic discovery is unavailable
    CACHED_AVAILABLE_MODELS = DEFAULT_MODEL_FALLBACK_LIST
    return CACHED_AVAILABLE_MODELS


def get_model_pool(api_key: str | None = None) -> list[str]:
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
    generation_config: dict | None = None,
    max_retries_per_model: int = 1,
    timeout: int | None = None,
) -> str:
    """Generate content using Gemini with automatic fallback across models."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment variables.")

    genai.configure(api_key=api_key)
    models = get_model_pool(api_key)
    last_error = None
    meaningful_error = None
    req_timeout = timeout or REQUEST_TIMEOUT

    for model_name in models:
        for attempt in range(max_retries_per_model + 1):
            try:
                logger.info(f"⚡ Gemini → {model_name} (attempt {attempt + 1}, timeout {req_timeout}s)")
                model = genai.GenerativeModel(model_name)
                kwargs = {"request_options": {"timeout": req_timeout}}
                if generation_config:
                    kwargs["generation_config"] = generation_config

                response = model.generate_content(contents, **kwargs)
                if response and response.text:
                    logger.info(f"✅ Success with {model_name}")
                    return response.text.strip()
                else:
                    raise RuntimeError("Gemini returned an empty response.")

            except ResourceExhausted as e:
                logger.warning(f"429 quota hit on '{model_name}' → switching model")
                last_error = e
                meaningful_error = e
                break

            except GoogleAPICallError as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                    logger.warning(f"Rate limited on '{model_name}' → switching model")
                    last_error = e
                    meaningful_error = e
                    break
                logger.error(f"API error on '{model_name}': {e}")
                last_error = e
                if "404" in err_str or "not found" in err_str:
                    # Non-existent model on this API version; skip without polluting meaningful_error
                    break
                meaningful_error = e
                time.sleep(0.5)

            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str:
                    logger.warning(f"Quota error on '{model_name}' → switching model")
                    last_error = e
                    meaningful_error = e
                    break
                if "deadline" in err_str or "timeout" in err_str or "timed out" in err_str:
                    logger.warning(f"Timeout on '{model_name}' after {req_timeout}s → switching model")
                    last_error = e
                    meaningful_error = e
                    break
                logger.error(f"Unexpected error with '{model_name}': {e}")
                last_error = e
                meaningful_error = e
                time.sleep(0.5)

    err_to_report = meaningful_error or last_error
    err_message = (
        "AI rate limit reached across all available Gemini models. "
        "Please wait 30–60 seconds before retrying. "
        f"(Underlying error: {err_to_report})"
    )
    raise RuntimeError(err_message)
