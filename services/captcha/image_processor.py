"""
services/captcha/image_processor.py
Processes and distorts image bank files on-the-fly:
- Strips EXIF metadata completely.
- Applies subtle random cropping/jitter.
- Adds subtle brightness and contrast variation.
- Injects light digital noise to disrupt perceptual hashing.
- Returns optimized JPEG bytes.
"""

import io
import os
import random
from PIL import Image, ImageEnhance
from captcha_config import IMAGE_SIZE

def process_captcha_image(image_path: str) -> bytes:
    """Loads an image from the bank, applies security distortion, and returns clean JPEG bytes."""
    if not os.path.exists(image_path):
        # Fallback placeholder if missing
        img = Image.new("RGB", IMAGE_SIZE, color=(120, 125, 130))
    else:
        # Load and discard any EXIF
        with Image.open(image_path) as src:
            img = src.convert("RGB")

    w, h = img.size

    # 1. Random subtle crop (2 to 8 pixels)
    crop_margin_x = random.randint(2, 8)
    crop_margin_y = random.randint(2, 8)
    crop_box = (
        crop_margin_x,
        crop_margin_y,
        w - crop_margin_x,
        h - crop_margin_y
    )
    img = img.crop(crop_box).resize(IMAGE_SIZE, Image.Resampling.BILINEAR)

    # 2. Subtle brightness adjustment (+/- 7%)
    brightness_factor = random.uniform(0.93, 1.07)
    img = ImageEnhance.Brightness(img).enhance(brightness_factor)

    # 3. Subtle contrast adjustment (+/- 7%)
    contrast_factor = random.uniform(0.93, 1.07)
    img = ImageEnhance.Contrast(img).enhance(contrast_factor)

    # 4. Subtle pixel noise injection on a subset of pixels
    pixels = img.load()
    num_noisy_pixels = int(w * h * 0.015)  # 1.5% of pixels
    for _ in range(num_noisy_pixels):
        rx = random.randint(0, w - 1)
        ry = random.randint(0, h - 1)
        r, g, b = pixels[rx, ry]
        delta = random.randint(-18, 18)
        pixels[rx, ry] = (
            max(0, min(255, r + delta)),
            max(0, min(255, g + delta)),
            max(0, min(255, b + delta))
        )

    # 5. Output clean JPEG without EXIF
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=88, optimize=True)
    output.seek(0)
    return output.getvalue()
