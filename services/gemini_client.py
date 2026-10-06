import os
import time
import logging
import json
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPICallError

logger = logging.getLogger(__name__)

# ── Speed-optimised fallback order ──────────────────────────────────────────
# Fastest → slowest.  Google AI Studio free-tier quotas are tracked PER MODEL.
# • gemini-2.0-flash   — 15 RPM, fast, great multimodal quality
# • gemini-1.5-flash-8b — 15 RPM, lightest/fastest model available
# • gemini-1.5-flash    — 15 RPM, reliable workhorse
# • gemini-2.5-flash    — 15 RPM, but "thinking" model → can be slow
# • gemini-3.6-flash    — only 5 RPM, last resort
DEFAULT_MODEL_FALLBACK_LIST = [
    "gemini-2.0-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash",
    "gemini-2.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3.1-pro",
]

# Per-request timeout (seconds).  Prevents a single slow model from blocking
# the entire fallback chain.  Override via GEMINI_TIMEOUT env var.
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "25"))


def get_model_pool() -> list[str]:
    """Return model list, prioritizing any user-specified model from GEMINI_MODEL."""
    preferred = os.getenv("GEMINI_MODEL", "").strip()
    pool = []
    if preferred:
        pool.append(preferred)
    for m in DEFAULT_MODEL_FALLBACK_LIST:
        if m not in pool:
            pool.append(m)
    return pool


def generate_content_with_retry(
    contents,
    generation_config: dict | None = None,
    max_retries_per_model: int = 1,
    timeout: int | None = None,
) -> str:
    """Generate content using Gemini with automatic fallback across models.

    Fast-path design:
    • Each model gets at most `timeout` seconds (default 25 s) before we move on.
    • On 429 / quota errors we switch models immediately (no wait).
    • Total worst-case ≈ len(pool) × timeout, but usually the first model wins.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment variables.")

    genai.configure(api_key=api_key)
    models = get_model_pool()
    last_error = None
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
                break  # next model immediately

            except GoogleAPICallError as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                    logger.warning(f"Rate limited on '{model_name}' → switching model")
                    last_error = e
                    break
                logger.error(f"API error on '{model_name}': {e}")
                last_error = e
                if "404" in err_str or "not found" in err_str:
                    break
                time.sleep(0.5)

            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str:
                    logger.warning(f"Quota error on '{model_name}' → switching model")
                    last_error = e
                    break
                if "deadline" in err_str or "timeout" in err_str or "timed out" in err_str:
                    logger.warning(f"Timeout on '{model_name}' after {req_timeout}s → switching model")
                    last_error = e
                    break
                logger.error(f"Unexpected error with '{model_name}': {e}")
                last_error = e
                time.sleep(0.5)

    err_message = (
        "AI rate limit reached across all available Gemini models. "
        "Please wait 30–60 seconds before retrying. "
        f"(Underlying error: {last_error})"
    )
    raise RuntimeError(err_message)
