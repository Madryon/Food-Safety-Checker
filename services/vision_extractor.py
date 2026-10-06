import io
import json
import logging
import os
from PIL import Image
from services.gemini_client import generate_content_with_retry

logger = logging.getLogger(__name__)

# ── Image optimisation settings ─────────────────────────────────────────────
MAX_DIMENSION = 1024   # px – keeps quality fine for text/labels
JPEG_QUALITY = 80      # good balance of size vs readability


def _optimize_image(image_path: str) -> Image.Image:
    """Open, resize, and compress an image so Gemini processes it faster."""
    img = Image.open(image_path)

    # Convert RGBA/P → RGB (JPEG doesn't support alpha)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    # Down-scale large images
    w, h = img.size
    if max(w, h) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        logger.info(f"📐 Resized image {w}×{h} → {img.size[0]}×{img.size[1]}")

    # Re-compress into an in-memory JPEG to shrink payload
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    buf.seek(0)
    return Image.open(buf)


def extract(image_path: str) -> dict:
    """Send image to Gemini Vision and extract product information.

    Uses automatic multi-model fallback on 429 / timeout.
    Image is resized + compressed first for speed.
    """
    try:
        img = _optimize_image(image_path)
    except Exception as e:
        return {"error": f"Invalid image file: {e}"}

    prompt = """You are a food product identifier for the Indian market.

Look at this packaged food product image and extract:
1. brand – Company/brand name (infer from knowledge if unclear)
2. product_name – Specific product name
3. ingredients_text – Ingredients from the label. If NOT visible, prefix with "[AI-sourced] " and provide typical known ingredients.
4. nutrition_facts – Per 100g dict with keys: energy_kcal, protein_g, total_fat_g, saturated_fat_g, trans_fat_g, carbohydrates_g, total_sugar_g, sodium_mg, fiber_g. Use known values if not visible. null only if unknown.
5. data_source – "image", "ai_knowledge", or "partial"

RULES:
- ALWAYS identify the product. Use your knowledge of Indian brands (Maggi, Parle-G, Britannia, Amul, Haldiram's, Lays, Kurkure, etc.)
- NEVER leave ingredients or nutrition empty.
- If NOT a food product: {"error": "Not a food product image. Please upload a packaged food item photo."}

Return ONLY raw JSON (no markdown code blocks) with keys: brand, product_name, ingredients_text, nutrition_facts, data_source"""

    try:
        text = generate_content_with_retry([prompt, img], timeout=30)

        # Clean up potential markdown wrapping
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "Could not parse the AI response. Please try again with a clearer photo."}
    except Exception as e:
        logger.error(f"Vision analysis failed: {e}")
        return {"error": f"Vision analysis failed: {str(e)}"}
