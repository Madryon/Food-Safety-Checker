import logging
from typing import Dict, Any
from services.ai_provider import extract_food_vision, _optimize_image_for_vision

logger = logging.getLogger(__name__)

# Expose _optimize_image for backwards-compatibility
_optimize_image = _optimize_image_for_vision


def extract(image_path: str) -> Dict[str, Any]:
    """Send image to multi-provider vision pipeline and extract product information.
    
    Features:
    - Automatically uses Google Gemini Multi-Key Pool (GEMINI_API_KEYS / GEMINI_API_KEY_1..5)
      with auto-rotation across models & keys.
    - Automatic fallback to Groq Vision (llama-3.2-11b-vision-preview) and OpenAI Vision if configured.
    - Image is optimized (resized + compressed) before upload for rapid throughput.
    """
    return extract_food_vision(image_path)
