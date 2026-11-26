import logging
from typing import Dict, Any
from services.ai_provider import (
    extract_food_vision,
    extract_food_vision_bytes,
    _optimize_image_for_vision,
)

logger = logging.getLogger(__name__)

# Expose _optimize_image for backwards-compatibility
_optimize_image = _optimize_image_for_vision


def extract(image_path: str) -> Dict[str, Any]:
    """Send image file to multi-provider vision pipeline and extract product information."""
    return extract_food_vision(image_path)


def extract_bytes(image_bytes: bytes) -> Dict[str, Any]:
    """Send raw image bytes to multi-provider vision pipeline — zero disk I/O."""
    return extract_food_vision_bytes(image_bytes)
