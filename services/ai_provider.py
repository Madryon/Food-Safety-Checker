import os
import time
import json
import base64
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
import requests
from PIL import Image
import io

from services.gemini_client import (
    generate_content_with_retry,
    get_gemini_api_keys,
    get_active_gemini_keys,
)

logger = logging.getLogger(__name__)

# ── Endpoints ──────────────────────────────────────────────────────────────
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# ── Default Recommended Models ─────────────────────────────────────────────
DEFAULT_GROQ_HEALTH_MODEL = os.getenv("GROQ_HEALTH_MODEL") or os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile"
DEFAULT_GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL") or "llama-3.2-11b-vision-preview"
DEFAULT_OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "meta-llama/llama-3.3-70b-instruct:free"
DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

# ── Rotation trackers & cooldown registries ────────────────────────────────
_GROQ_KEY_COOLDOWNS: Dict[str, float] = {}
_LAST_GROQ_KEY_INDEX = 0


# ── Key Extraction Helpers ──────────────────────────────────────────────────
def get_groq_api_keys() -> List[str]:
    """Retrieve all configured Groq API keys from environment variables."""
    keys: List[str] = []
    raw = os.getenv("GROQ_API_KEYS", "").strip()
    if raw:
        for k in re.split(r"[,;\n\s]+", raw):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    k = os.getenv("GROQ_API_KEY", "").strip()
    if k and k not in keys:
        keys.append(k)
    for i in range(1, 11):
        k = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def get_openrouter_api_keys() -> List[str]:
    """Retrieve OpenRouter API keys from environment variables."""
    keys: List[str] = []
    raw = os.getenv("OPENROUTER_API_KEYS", "").strip()
    if raw:
        for k in re.split(r"[,;\n\s]+", raw):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    k = os.getenv("OPENROUTER_API_KEY", "").strip()
    if k and k not in keys:
        keys.append(k)
    return keys


def get_openai_api_keys() -> List[str]:
    """Retrieve OpenAI API keys from environment variables."""
    keys: List[str] = []
    raw = os.getenv("OPENAI_API_KEYS", "").strip()
    if raw:
        for k in re.split(r"[,;\n\s]+", raw):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    k = os.getenv("OPENAI_API_KEY", "").strip()
    if k and k not in keys:
        keys.append(k)
    return keys


def get_ai_status_summary() -> Dict[str, Any]:
    """Provide a comprehensive diagnostic breakdown of all configured AI providers.
    Useful for health checks, debugging, and admin telemetry.
    """
    gemini_keys = get_gemini_api_keys()
    groq_keys = get_groq_api_keys()
    openrouter_keys = get_openrouter_api_keys()
    openai_keys = get_openai_api_keys()

    total_keys = len(gemini_keys) + len(groq_keys) + len(openrouter_keys) + len(openai_keys)

    providers = []
    if groq_keys:
        providers.append(f"Groq Cloud ({len(groq_keys)} key{'s' if len(groq_keys)>1 else ''}) [Ultra-Fast ~300ms]")
    if gemini_keys:
        providers.append(f"Google Gemini Pool ({len(gemini_keys)} key{'s' if len(gemini_keys)>1 else ''})")
    if openrouter_keys:
        providers.append(f"OpenRouter ({len(openrouter_keys)} key{'s' if len(openrouter_keys)>1 else ''})")
    if openai_keys:
        providers.append(f"OpenAI ({len(openai_keys)} key{'s' if len(openai_keys)>1 else ''})")

    return {
        "status": "ready" if total_keys > 0 else "no_ai_keys_configured",
        "total_keys_count": total_keys,
        "gemini_keys_count": len(gemini_keys),
        "groq_keys_count": len(groq_keys),
        "openrouter_configured": bool(openrouter_keys),
        "openai_configured": bool(openai_keys),
        "providers_active": providers,
        "ultra_fast_groq_enabled": bool(groq_keys),
        "primary_health_analyst": "Groq LPU (Ultra-Fast ~300ms)" if groq_keys else "Gemini Multi-Key Pool",
        "primary_vision_ocr": "Gemini Multi-Key Pool (High Precision OCR)" if gemini_keys else ("Groq Vision" if groq_keys else "None"),
    }


# ── Groq API Calling Utilities ─────────────────────────────────────────────
def _get_active_groq_keys() -> List[str]:
    all_keys = get_groq_api_keys()
    if not all_keys:
        return []
    now = time.time()
    active = [k for k in all_keys if _GROQ_KEY_COOLDOWNS.get(k, 0) <= now]
    return active if active else all_keys


def call_groq_json(
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    timeout: int = 15,
    max_tokens: int = 2048,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Invoke Groq Cloud with strict JSON output formatting.
    Returns: (parsed_json_dict, raw_content_str, error_message)
    """
    global _LAST_GROQ_KEY_INDEX
    keys = _get_active_groq_keys()
    if not keys:
        return None, None, "No GROQ_API_KEY configured."

    chosen_model = model or DEFAULT_GROQ_HEALTH_MODEL
    total_keys = len(keys)
    start_idx = _LAST_GROQ_KEY_INDEX % total_keys
    ordered_keys = keys[start_idx:] + keys[:start_idx]
    _LAST_GROQ_KEY_INDEX = (start_idx + 1) % total_keys

    for key_idx, key in enumerate(ordered_keys):
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": chosen_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }

        try:
            logger.info(f"⚡ Groq Cloud → {chosen_model} (key ...{key[-4:] if len(key)>6 else 'key'})")
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                parsed = json.loads(content)
                logger.info(f"✅ Groq success in {resp.elapsed.total_seconds():.2f}s with {chosen_model}")
                return parsed, content, None
            
            # Quota or rate-limit: mark cooldown and rotate key
            if resp.status_code in (429, 413, 503):
                _GROQ_KEY_COOLDOWNS[key] = time.time() + 60
                logger.warning(f"Groq rate limit ({resp.status_code}) on key ...{key[-4:] if len(key)>6 else 'key'}. Rotating...")
                continue
            
            logger.warning(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as ex:
            logger.warning(f"Groq call failed on key ...{key[-4:] if len(key)>6 else 'key'}: {ex}")
            continue

    return None, None, "All Groq keys failed or rate-limited."


# ── OpenRouter / OpenAI Calling Utilities ──────────────────────────────────
def call_openai_compatible_json(
    api_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    timeout: int = 20,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Generic caller for OpenAI-compatible JSON APIs (OpenRouter, OpenAI)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter" in api_url:
        headers["HTTP-Referer"] = "https://foodcheck.local"
        headers["X-Title"] = "FoodCheck AI"

    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 2048,
    }

    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # Clean markdown codeblocks if model added them
            clean_content = _clean_markdown_json(content)
            parsed = json.loads(clean_content)
            return parsed, content, None
        return None, None, f"HTTP {resp.status_code}: {resp.text[:150]}"
    except Exception as ex:
        return None, None, str(ex)


def _clean_markdown_json(text: str) -> str:
    """Safely strip markdown code blocks and surrounding whitespace."""
    s = text.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


# ── Unified Food Health & Safety Analysis ──────────────────────────────────
def analyze_food_health(
    ingredients_text: str,
    nutrition_facts: Optional[Dict[str, Any]],
    product_name: str = "",
) -> Dict[str, Any]:
    """Perform food safety assessment and FSSAI health score analysis.
    
    Multi-Provider Strategy:
    1. Groq Cloud (if key provided): Blazing fast (~300ms) with Llama-3.3-70B.
    2. Google Gemini Multi-Key Pool: Rotating across all configured Gemini keys & models.
    3. OpenRouter / OpenAI: Robust external backup fallback.
    
    Guarantees strict JSON schema with keys:
    - score (0-100)
    - verdict ('safe' | 'moderate' | 'unsafe')
    - flags (list of {type, severity, explanation})
    - reasoning (str)
    - alternatives (list of {name, category, why_better})
    """
    start_time = time.time()
    nutrition_str = json.dumps(nutrition_facts, indent=2) if nutrition_facts else "Not available"

    system_instruction = (
        "You are an expert food safety analyst specializing in Indian packaged foods and FSSAI regulations. "
        "Evaluate the product's health and safety. Always respond with pure valid JSON matching the requested schema."
    )

    user_prompt = f"""Analyze the following packaged food product and provide a health/safety assessment.

Product Name: {product_name}

Ingredients:
{ingredients_text}

Nutrition Facts (per 100g):
{nutrition_str}

=== SCORING RULES ===
Score from 0 to 100, where 100 is healthiest.

Flag and explain if ANY of the following are present:
1. Added sugar or total sugar > 10g per 100g — flag as HIGH SUGAR
2. Palm oil or palm kernel oil — flag as PALM OIL (environmental and health concern)
3. Hydrogenated oil or trans fat > 0g — flag as TRANS FAT / HYDROGENATED OIL
4. Artificial colors or preservatives (INS codes like INS 110, INS 211, etc.) — flag as ARTIFICIAL ADDITIVES
5. Protein content misleading vs. product's health claims — flag as MISLEADING CLAIMS
6. Allergens present (nuts, dairy, gluten, soy) — flag as ALLERGEN with specifics
7. High sodium (> 500mg per 100g) — flag as HIGH SODIUM
8. Ultra-processed indicators (long ingredient list with many chemical additives) — flag as ULTRA-PROCESSED

=== VERDICT MAPPING ===
- Score 70-100: verdict = "safe"
- Score 40-69: verdict = "moderate" 
- Score 0-39: verdict = "unsafe"

=== ALTERNATIVES SUGGESTION ===
Suggest 2 to 3 healthier or cleaner alternative products or food options available in the Indian market for this specific product or category.

=== OUTPUT FORMAT ===
Return ONLY valid JSON with exactly these keys:
{{
  "score": <integer 0-100>,
  "verdict": "<safe|moderate|unsafe>",
  "flags": [
    {{
      "type": "<FLAG_TYPE>",
      "severity": "<high|medium|low>",
      "explanation": "<brief explanation>"
    }}
  ],
  "reasoning": "<2-3 sentence overall assessment>",
  "alternatives": [
    {{
      "name": "<Name of healthier alternative brand/product or swap>",
      "category": "<Healthier Swap|Clean Label Brand|Natural Whole Food>",
      "why_better": "<1-2 sentence explanation of why it is significantly healthier>"
    }}
  ]
}}
Raw JSON only. No markdown formatting."""

    provider_used = None
    parsed_result = None
    last_error = None

    # ── 1. FAST-PATH: Try Groq Cloud first if available (~300ms latency) ───
    groq_keys = get_groq_api_keys()
    if groq_keys:
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ]
        groq_result, _, err = call_groq_json(messages, model=DEFAULT_GROQ_HEALTH_MODEL, timeout=15)
        if groq_result and _validate_health_schema(groq_result):
            parsed_result = groq_result
            provider_used = f"Groq/{DEFAULT_GROQ_HEALTH_MODEL}"
        elif err:
            logger.warning(f"Groq fast-path failed: {err}. Falling back to Gemini multi-key pool.")

    # ── 2. STANDARD-PATH: Gemini Multi-Key Pool (rotating keys & models) ───
    if not parsed_result:
        gemini_keys = get_gemini_api_keys()
        if gemini_keys:
            try:
                full_prompt = f"{system_instruction}\n\n{user_prompt}"
                raw_text = generate_content_with_retry(full_prompt, timeout=25)
                clean_text = _clean_markdown_json(raw_text)
                gemini_data = json.loads(clean_text)
                if _validate_health_schema(gemini_data):
                    parsed_result = gemini_data
                    provider_used = "Google Gemini Multi-Key Pool"
            except Exception as ex:
                logger.warning(f"Gemini multi-key pool attempt failed: {ex}")
                last_error = str(ex)

    # ── 3. RESCUE-PATH: OpenRouter / OpenAI ─────────────────────────────────
    if not parsed_result:
        openrouter_keys = get_openrouter_api_keys()
        if openrouter_keys:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ]
            res, _, err = call_openai_compatible_json(
                OPENROUTER_API_URL, openrouter_keys[0], DEFAULT_OPENROUTER_MODEL, messages, timeout=20
            )
            if res and _validate_health_schema(res):
                parsed_result = res
                provider_used = f"OpenRouter/{DEFAULT_OPENROUTER_MODEL}"

    if not parsed_result:
        openai_keys = get_openai_api_keys()
        if openai_keys:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ]
            res, _, err = call_openai_compatible_json(
                OPENAI_API_URL, openai_keys[0], DEFAULT_OPENAI_MODEL, messages, timeout=20
            )
            if res and _validate_health_schema(res):
                parsed_result = res
                provider_used = f"OpenAI/{DEFAULT_OPENAI_MODEL}"

    # ── 4. FINAL VALIDATION & STANDARDIZATION ──────────────────────────────
    elapsed_ms = int((time.time() - start_time) * 1000)

    if parsed_result:
        _normalize_health_result(parsed_result)
        parsed_result["_ai_provider"] = provider_used
        parsed_result["_latency_ms"] = elapsed_ms
        logger.info(f"⚡ Health score generated via {provider_used} in {elapsed_ms}ms (Score: {parsed_result['score']})")
        return parsed_result

    # Safe fallback response if all AI providers exhausted
    logger.error(f"All AI health providers exhausted. Last error: {last_error}")
    return _health_fallback_response(
        last_error or "All AI provider quotas are currently in cooldown. Please retry in 30 seconds."
    )


def _validate_health_schema(data: Any) -> bool:
    """Check that essential keys exist in health analysis response."""
    if not isinstance(data, dict):
        return False
    required = {"score", "verdict", "flags", "reasoning"}
    return required.issubset(data.keys())


def _normalize_health_result(res: Dict[str, Any]):
    """Ensure proper types and consistent values for health result fields."""
    try:
        res["score"] = max(0, min(100, int(res.get("score", 50))))
    except (ValueError, TypeError):
        res["score"] = 50

    if res.get("verdict") not in ("safe", "moderate", "unsafe"):
        if res["score"] >= 70:
            res["verdict"] = "safe"
        elif res["score"] >= 40:
            res["verdict"] = "moderate"
        else:
            res["verdict"] = "unsafe"

    if not isinstance(res.get("flags"), list):
        res["flags"] = []
    if not isinstance(res.get("alternatives"), list):
        res["alternatives"] = []


def _health_fallback_response(error_msg: str) -> Dict[str, Any]:
    """Safe fallback response when all AI providers fail."""
    return {
        "score": 50,
        "verdict": "moderate",
        "flags": [
            {
                "type": "AI_ANALYSIS_OFFLINE",
                "severity": "medium",
                "explanation": f"Automated AI score temporarily unavailable ({error_msg}). Please review packaging manually.",
            }
        ],
        "reasoning": "Could not complete automated AI analysis due to rate limits across AI providers. Please check the label directly.",
        "alternatives": [
            {
                "name": "Fresh whole fruits or homemade snack",
                "category": "Natural Whole Food",
                "why_better": "Zero preservatives, added sugars, or industrial additives.",
            }
        ],
        "_ai_provider": "fallback",
    }


# ── Unified Food Packaging Vision Extraction ───────────────────────────────
def _optimize_image_for_vision(image_path: str, max_dimension: int = 1024, quality: int = 80) -> Image.Image:
    """Resize and compress image for rapid AI multimodal transmission."""
    with Image.open(image_path) as raw_img:
        if raw_img.mode in ("RGBA", "P", "LA"):
            img = raw_img.convert("RGB")
        else:
            img = raw_img.copy()

    w, h = img.size
    if max(w, h) > max_dimension:
        ratio = max_dimension / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.BILINEAR)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    buf.seek(0)
    opt_img = Image.open(buf)
    opt_img.load()
    return opt_img


def _image_to_base64_jpeg(image_path: str) -> str:
    """Convert an image to optimized JPEG base64 string."""
    opt_img = _optimize_image_for_vision(image_path)
    buf = io.BytesIO()
    opt_img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def extract_food_vision(image_path: str) -> Dict[str, Any]:
    """Extract product name, ingredients, and nutrition facts from packaging photo.
    
    Multi-Provider Strategy:
    1. Google Gemini Multi-Key Pool (Highest accuracy for packaging OCR).
    2. Groq Vision (llama-3.2-11b-vision-preview via base64 data URL).
    3. OpenAI Vision (gpt-4o-mini).
    """
    vision_prompt = """You are a food product identifier for the Indian market.

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

    # ── 1. Gemini Multi-Key Pool (Primary OCR) ───
    gemini_keys = get_gemini_api_keys()
    if gemini_keys:
        try:
            opt_img = _optimize_image_for_vision(image_path)
            raw_text = generate_content_with_retry([vision_prompt, opt_img], timeout=30)
            clean_text = _clean_markdown_json(raw_text)
            data = json.loads(clean_text)
            if "error" not in data or data.get("brand") or data.get("product_name"):
                return data
        except Exception as ex:
            logger.warning(f"Gemini vision extraction failed: {ex}. Attempting vision fallback...")

    # ── 2. Groq Vision Fallback (llama-3.2-11b-vision-preview) ───
    groq_keys = get_groq_api_keys()
    if groq_keys:
        try:
            b64_img = _image_to_base64_jpeg(image_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                    ],
                }
            ]
            res, _, err = call_groq_json(messages, model=DEFAULT_GROQ_VISION_MODEL, timeout=25)
            if res:
                return res
        except Exception as ex:
            logger.warning(f"Groq vision extraction failed: {ex}")

    # ── 3. OpenAI Vision Fallback (gpt-4o-mini) ───
    openai_keys = get_openai_api_keys()
    if openai_keys:
        try:
            b64_img = _image_to_base64_jpeg(image_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                    ],
                }
            ]
            res, _, err = call_openai_compatible_json(
                OPENAI_API_URL, openai_keys[0], "gpt-4o-mini", messages, timeout=25
            )
            if res:
                return res
        except Exception as ex:
            logger.warning(f"OpenAI vision extraction failed: {ex}")

    return {
        "error": "AI vision analysis timed out across all available providers. Please re-take a clear photo or enter product details manually."
    }
