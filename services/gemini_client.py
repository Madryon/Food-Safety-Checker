import os
import time
import logging
import json
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPICallError

logger = logging.getLogger(__name__)

# Models in order of reliability and free-tier quota availability.
# Google AI Studio tracks free-tier quotas PER MODEL.
# Production models (gemini-2.5-flash, gemini-2.0-flash, gemini-1.5-flash) offer 15 RPM.
# Preview/Experimental models (like gemini-3.6-flash) are often capped at only 5 RPM.
DEFAULT_MODEL_FALLBACK_LIST = [
    "gemini-3.7-flash",
    "gemini-3.1-pro",
    "gemini-3.8-flash",
]


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
) -> str:
    """Generate content using Gemini with automatic fallback across models when 429 quota is hit.

    Args:
        contents: String prompt or list of [prompt, image].
        generation_config: Optional generation configuration dict.
        max_retries_per_model: Number of retries before switching to the next model.

    Returns:
        The raw text response from the model.

    Raises:
        RuntimeError if all models in the pool fail.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment variables.")

    genai.configure(api_key=api_key)
    models = get_model_pool()
    last_error = None

    for model_name in models:
        for attempt in range(max_retries_per_model + 1):
            try:
                logger.info(f"Attempting Gemini generation with model: {model_name} (attempt {attempt + 1})")
                model = genai.GenerativeModel(model_name)
                kwargs = {}
                if generation_config:
                    kwargs["generation_config"] = generation_config

                response = model.generate_content(contents, **kwargs)
                if response and response.text:
                    return response.text.strip()
                else:
                    raise RuntimeError("Gemini returned an empty response.")

            except ResourceExhausted as e:
                logger.warning(f"Quota exceeded (429) for model '{model_name}': {e}. Switching to fallback model.")
                last_error = e
                # Don't wait 30s — switch to the next model immediately since quota is per-model!
                break

            except GoogleAPICallError as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                    logger.warning(f"Rate limited on model '{model_name}': {e}. Switching to fallback model.")
                    last_error = e
                    break
                logger.error(f"Google API call error on model '{model_name}': {e}")
                last_error = e
                # If not found or deprecated model, switch to next model
                if "404" in err_str or "not found" in err_str:
                    break
                time.sleep(1)

            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str:
                    logger.warning(f"Quota error on model '{model_name}': {e}. Switching to fallback model.")
                    last_error = e
                    break
                logger.error(f"Unexpected error with model '{model_name}': {e}")
                last_error = e
                time.sleep(1)

    # If all models in the fallback pool were exhausted
    err_message = (
        "AI rate limit reached across all available Gemini models. "
        "Please wait 30–60 seconds before retrying. "
        f"(Underlying error: {last_error})"
    )
    raise RuntimeError(err_message)
