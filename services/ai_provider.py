from dotenv import load_dotenv
load_dotenv()
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



# ── Comprehensive Food Product Knowledge Base (Instant Fallback) ────────────
_FOOD_KNOWLEDGE_BASE = [
    {
        "keywords": ["maggi", "nestle noodle", "2-minute noodle", "instant noodle", "tastemaker"],
        "product_name": "Nestle Maggi 2-Minute Masala Instant Noodles",
        "brand": "Nestle",
        "category": "Instant Noodles",
        "ingredients_text": "Noodles: Refined wheat flour (Maida), Palm oil, Iodised salt, Wheat gluten, Mineral (Calcium carbonate), Thickeners (INS 508, INS 412), Acidity regulators (INS 501(i), INS 500(i)). Masala Tastemaker: Hydrolysed groundnut protein, Mixed spices (23.6%) (Dehydrated onion, Coriander powder, Turmeric, Red chilli, Cumin, Aniseed, Fenugreek, Ginger, Black pepper, Clove, Nutmeg, Cardamom), Noodle powder, Sugar, Edible starch, Refined palm oil, Thickener (INS 508), Caramel (INS 150d), Flavour enhancer (INS 635).",
        "nutrition_facts": {
            "energy_kcal": 427,
            "protein_g": 8.0,
            "total_fat_g": 15.7,
            "saturated_fat_g": 6.8,
            "trans_fat_g": 0.12,
            "carbohydrates_g": 63.5,
            "total_sugar_g": 2.2,
            "sodium_mg": 860,
            "fiber_g": 3.6
        },
        "allergens": ["Wheat (Gluten)", "Groundnut/Peanut"],
        "claims": ["100% Vegetarian", "Source of Iron and Calcium"]
    },
    {
        "keywords": ["good day", "britannia cookie", "butter cookie", "butter biscuit"],
        "product_name": "Britannia Good Day Butter Cookies",
        "brand": "Britannia",
        "category": "Biscuits & Cookies",
        "ingredients_text": "Refined Wheat Flour (Maida), Sugar, Refined Palm Oil, Butter (2%), Invert Sugar Syrup, Milk Solids, Iodised Salt, Raising Agents (INS 503(ii), INS 500(ii)), Emulsifiers (INS 322 from Soy, INS 471), Nature Identical and Artificial Flavouring Substances (Butter, Vanilla).",
        "nutrition_facts": {
            "energy_kcal": 492,
            "protein_g": 7.0,
            "total_fat_g": 22.0,
            "saturated_fat_g": 10.5,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 66.5,
            "total_sugar_g": 23.0,
            "sodium_mg": 320,
            "fiber_g": 1.5
        },
        "allergens": ["Wheat (Gluten)", "Milk", "Soy"],
        "claims": ["100% Vegetarian", "Made with Real Butter"]
    },
    {
        "keywords": ["parle-g", "parle g", "glucose biscuit", "glucose"],
        "product_name": "Parle-G Original Glucose Biscuits",
        "brand": "Parle",
        "category": "Biscuits & Cookies",
        "ingredients_text": "Refined Wheat Flour (Maida) (67%), Sugar (23.5%), Refined Palm Oil, Invert Sugar Syrup, Iodised Salt, Raising Agents (INS 503(ii), INS 500(ii)), Milk Solids, Emulsifiers (INS 472e), Dough Conditioner (INS 223), Artificial Flavouring Substances (Vanilla).",
        "nutrition_facts": {
            "energy_kcal": 451,
            "protein_g": 6.5,
            "total_fat_g": 12.5,
            "saturated_fat_g": 5.8,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 78.0,
            "total_sugar_g": 26.0,
            "sodium_mg": 280,
            "fiber_g": 2.0
        },
        "allergens": ["Wheat (Gluten)", "Milk", "Sulphites"],
        "claims": ["100% Vegetarian", "G for Genius"]
    },
    {
        "keywords": ["oreo", "cream biscuit", "vanilla creme", "chocolate sandwich"],
        "product_name": "Cadbury Oreo Vanilla Creme Sandwich Biscuits",
        "brand": "Cadbury",
        "category": "Biscuits & Cookies",
        "ingredients_text": "Refined Wheat Flour (Maida), Sugar (37%), Refined Palm Oil, Cocoa Solids (4.3%), Invert Sugar Syrup, Starch, Raising Agents (INS 500(ii), INS 503(ii)), Iodised Salt, Emulsifier (INS 322 from Soy), Nature Identical Flavouring Substances (Vanilla, Chocolate).",
        "nutrition_facts": {
            "energy_kcal": 482,
            "protein_g": 5.2,
            "total_fat_g": 19.5,
            "saturated_fat_g": 9.4,
            "trans_fat_g": 0.1,
            "carbohydrates_g": 71.5,
            "total_sugar_g": 38.5,
            "sodium_mg": 410,
            "fiber_g": 2.2
        },
        "allergens": ["Wheat (Gluten)", "Soy"],
        "claims": ["100% Vegetarian"]
    },
    {
        "keywords": ["lays", "lay's", "magic masala", "potato chips", "blue lays"],
        "product_name": "Lay's India's Magic Masala Potato Chips",
        "brand": "PepsiCo",
        "category": "Chips & Crisps",
        "ingredients_text": "Potato, Edible Vegetable Oil (Palmolein), Seasoning (Spices and Condiments: Onion powder, Chilli powder, Dry mango powder, Coriander powder, Black pepper, Cumin, Nutmeg, Cinnamon), Sugar, Iodised Salt, Tomato powder, Citric Acid (INS 330), Flavour Enhancers (INS 627, INS 631).",
        "nutrition_facts": {
            "energy_kcal": 555,
            "protein_g": 7.0,
            "total_fat_g": 35.7,
            "saturated_fat_g": 14.5,
            "trans_fat_g": 0.1,
            "carbohydrates_g": 51.5,
            "total_sugar_g": 4.5,
            "sodium_mg": 790,
            "fiber_g": 3.8
        },
        "allergens": [],
        "claims": ["100% Vegetarian"]
    },
    {
        "keywords": ["kurkure", "masala munch", "corn puff"],
        "product_name": "Kurkure Masala Munch Crispy Snack",
        "brand": "PepsiCo",
        "category": "Extruded Snacks",
        "ingredients_text": "Rice Meal (43.2%), Edible Vegetable Oil (Palmolein), Corn Meal (20%), Gram Meal (3.3%), Seasoning (Spices and Condiments: Onion powder, Red chilli, Coriander, Garlic, Black pepper, Turmeric), Sugar, Iodised Salt, Acidity Regulators (INS 330), Flavour Enhancers (INS 627, INS 631).",
        "nutrition_facts": {
            "energy_kcal": 561,
            "protein_g": 6.2,
            "total_fat_g": 36.3,
            "saturated_fat_g": 15.2,
            "trans_fat_g": 0.1,
            "carbohydrates_g": 52.4,
            "total_sugar_g": 2.5,
            "sodium_mg": 840,
            "fiber_g": 2.9
        },
        "allergens": [],
        "claims": ["100% Vegetarian", "Cooked in Palmolein"]
    },
    {
        "keywords": ["dairy milk", "cadbury chocolate", "milk chocolate", "silk"],
        "product_name": "Cadbury Dairy Milk Chocolate",
        "brand": "Cadbury",
        "category": "Chocolates & Confectionery",
        "ingredients_text": "Sugar, Milk Solids (16%), Cocoa Butter, Cocoa Solids, Emulsifiers (INS 442, INS 476), Nature Identical and Artificial Flavouring Substances (Vanilla).",
        "nutrition_facts": {
            "energy_kcal": 532,
            "protein_g": 7.8,
            "total_fat_g": 30.5,
            "saturated_fat_g": 18.5,
            "trans_fat_g": 0.2,
            "carbohydrates_g": 58.0,
            "total_sugar_g": 57.0,
            "sodium_mg": 140,
            "fiber_g": 2.5
        },
        "allergens": ["Milk"],
        "claims": ["100% Vegetarian", "With Glass and a Half of Pure Milk"]
    },
    {
        "keywords": ["kitkat", "kit kat", "wafer chocolate"],
        "product_name": "Nestle KitKat Crisp Wafer Bar in Milk Chocolate",
        "brand": "Nestle",
        "category": "Chocolates & Confectionery",
        "ingredients_text": "Sugar, Milk Solids, Refined Wheat Flour (Maida), Cocoa Butter, Cocoa Solids, Edible Vegetable Fats (Hydrogenated Palm Oil, Shea), Emulsifier (INS 322 from Soy), Yeast, Raising Agent (INS 500(ii)), Iodised Salt, Flour Treatment Agent (INS 516).",
        "nutrition_facts": {
            "energy_kcal": 512,
            "protein_g": 6.8,
            "total_fat_g": 26.5,
            "saturated_fat_g": 16.2,
            "trans_fat_g": 0.1,
            "carbohydrates_g": 62.0,
            "total_sugar_g": 48.0,
            "sodium_mg": 110,
            "fiber_g": 2.0
        },
        "allergens": ["Milk", "Wheat (Gluten)", "Soy"],
        "claims": ["100% Vegetarian", "Have a Break, Have a KitKat"]
    },
    {
        "keywords": ["amul butter", "pasteurised butter", "table butter"],
        "product_name": "Amul Pasteurised Salted Table Butter",
        "brand": "Amul",
        "category": "Dairy & Spreads",
        "ingredients_text": "Butter (derived from Cow/Buffalo Milk Fat 80%), Common Salt (2.5%), Permitted Natural Colour (INS 160a(i) - Beta Carotene).",
        "nutrition_facts": {
            "energy_kcal": 722,
            "protein_g": 0.6,
            "total_fat_g": 80.0,
            "saturated_fat_g": 51.0,
            "trans_fat_g": 3.0,
            "carbohydrates_g": 0.0,
            "total_sugar_g": 0.0,
            "sodium_mg": 830,
            "fiber_g": 0.0
        },
        "allergens": ["Milk"],
        "claims": ["100% Vegetarian", "Utterly Butterly Delicious"]
    },
    {
        "keywords": ["paneer", "cottage cheese", "amul paneer"],
        "product_name": "Amul Fresh Malai Paneer",
        "brand": "Amul",
        "category": "Dairy & Cheese",
        "ingredients_text": "Pasteurised Cow/Buffalo Milk, Citric Acid (INS 330).",
        "nutrition_facts": {
            "energy_kcal": 289,
            "protein_g": 18.0,
            "total_fat_g": 22.0,
            "saturated_fat_g": 14.0,
            "trans_fat_g": 0.5,
            "carbohydrates_g": 4.5,
            "total_sugar_g": 2.5,
            "sodium_mg": 40,
            "fiber_g": 0.0
        },
        "allergens": ["Milk"],
        "claims": ["Rich in Protein", "100% Pure Milk Paneer"]
    },
    {
        "keywords": ["peanut butter", "pintola", "myfitness"],
        "product_name": "Pintola All-Natural Roasted Peanut Butter",
        "brand": "Pintola",
        "category": "Nut Butters & Spreads",
        "ingredients_text": "100% Roasted Peanuts (Zero Added Sugar, Zero Hydrogenated Oils, Zero Added Salt).",
        "nutrition_facts": {
            "energy_kcal": 625,
            "protein_g": 30.0,
            "total_fat_g": 50.0,
            "saturated_fat_g": 8.0,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 18.0,
            "total_sugar_g": 5.0,
            "sodium_mg": 15,
            "fiber_g": 8.0
        },
        "allergens": ["Peanuts"],
        "claims": ["High Protein", "No Added Sugar", "100% Vegan", "Zero Cholesterol"]
    },
    {
        "keywords": ["frooti", "mango drink", "parle agro"],
        "product_name": "Frooti Fresh 'N' Juicy Mango Drink",
        "brand": "Parle Agro",
        "category": "Fruit Beverages",
        "ingredients_text": "Water, Mango Pulp (19.5%), Sugar (13.5%), Acidity Regulator (INS 330), Antioxidant (INS 300), Permitted Class II Preservatives (INS 211, INS 224), Nature Identical and Artificial Flavouring Substances (Mango).",
        "nutrition_facts": {
            "energy_kcal": 65,
            "protein_g": 0.2,
            "total_fat_g": 0.0,
            "saturated_fat_g": 0.0,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 16.2,
            "total_sugar_g": 15.5,
            "sodium_mg": 28,
            "fiber_g": 0.4
        },
        "allergens": ["Sulphites"],
        "claims": ["100% Vegetarian"]
    },
    {
        "keywords": ["coca-cola", "coke", "soft drink", "cola"],
        "product_name": "Coca-Cola Original Carbonated Beverage",
        "brand": "Coca-Cola",
        "category": "Carbonated Soft Drinks",
        "ingredients_text": "Carbonated Water, Sugar (10.6%), Acidity Regulator (INS 338), Colour (INS 150d), Natural Flavours, Caffeine (8.2mg/100ml).",
        "nutrition_facts": {
            "energy_kcal": 44,
            "protein_g": 0.0,
            "total_fat_g": 0.0,
            "saturated_fat_g": 0.0,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 10.6,
            "total_sugar_g": 10.6,
            "sodium_mg": 12,
            "fiber_g": 0.0
        },
        "allergens": [],
        "claims": ["Contains Caffeine", "100% Vegetarian"]
    },
    {
        "keywords": ["red bull", "energy drink"],
        "product_name": "Red Bull Energy Drink",
        "brand": "Red Bull",
        "category": "Energy Drinks",
        "ingredients_text": "Carbonated Water, Sucrose, Glucose, Citric Acid (INS 330), Taurine (0.4%), Sodium Bicarbonate, Magnesium Carbonate, Caffeine (30mg/100ml), Niacinamide, Pyridoxine HCl (B6), Riboflavin (B2), Vitamin B12, Natural Flavours, Caramel Colour (INS 150a).",
        "nutrition_facts": {
            "energy_kcal": 45,
            "protein_g": 0.0,
            "total_fat_g": 0.0,
            "saturated_fat_g": 0.0,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 11.0,
            "total_sugar_g": 11.0,
            "sodium_mg": 105,
            "fiber_g": 0.0
        },
        "allergens": [],
        "claims": ["Vitalizes Body and Mind", "High Caffeine Content"]
    },
    {
        "keywords": ["protinex", "protein powder", "health drink"],
        "product_name": "Danone Protinex Original Hydrolysed Protein Drink",
        "brand": "Danone",
        "category": "Health & Nutritional Supplements",
        "ingredients_text": "Soy Protein Isolate, Sucrose, Skimmed Milk Powder, Wheat Flour, Malt Extract, Minerals (Calcium, Phosphorus, Iron, Zinc), Vitamins (C, E, B-Complex), Taurine, Nature Identical Flavouring Substances.",
        "nutrition_facts": {
            "energy_kcal": 372,
            "protein_g": 34.0,
            "total_fat_g": 1.5,
            "saturated_fat_g": 0.5,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 56.0,
            "total_sugar_g": 28.0,
            "sodium_mg": 480,
            "fiber_g": 4.5
        },
        "allergens": ["Soy", "Milk", "Wheat (Gluten)"],
        "claims": ["High Protein 34%", "Zero Added Trans Fat", "Fortified with 25 Immuno-Nutrients"]
    },
    {
        "keywords": ["corn flakes", "kellogg's", "kelloggs", "breakfast cereal"],
        "product_name": "Kellogg's Corn Flakes Original",
        "brand": "Kellogg's",
        "category": "Breakfast Cereals",
        "ingredients_text": "Milled Corn (89%), Sugar, Malt Extract, Iodised Salt, Vitamins (C, B1, B2, B3, B6, Folate, B12), Mineral (Iron), Antioxidant (INS 307b).",
        "nutrition_facts": {
            "energy_kcal": 378,
            "protein_g": 7.5,
            "total_fat_g": 0.8,
            "saturated_fat_g": 0.2,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 85.0,
            "total_sugar_g": 8.5,
            "sodium_mg": 680,
            "fiber_g": 3.0
        },
        "allergens": ["Gluten (Barley Malt)"],
        "claims": ["Iron & B-Vitamins Rich", "99% Fat Free"]
    },
    {
        "keywords": ["oats", "quaker", "masala oats", "saffola oats", "rolled oats"],
        "product_name": "Quaker Rolled Wholegrain Oats",
        "brand": "Quaker",
        "category": "Breakfast Cereals",
        "ingredients_text": "100% Whole Grain Rolled Oats (Beta-Glucan Soluble Dietary Fibre).",
        "nutrition_facts": {
            "energy_kcal": 389,
            "protein_g": 12.5,
            "total_fat_g": 7.0,
            "saturated_fat_g": 1.3,
            "trans_fat_g": 0.0,
            "carbohydrates_g": 66.0,
            "total_sugar_g": 1.0,
            "sodium_mg": 8,
            "fiber_g": 10.5
        },
        "allergens": ["Oats (Gluten)"],
        "claims": ["100% Whole Grain", "Helps Reduce Cholesterol", "High Fibre"]
    }
]


def _generate_generic_food_profile(food: str, brand: str = "", variant: str = "") -> Dict[str, Any]:
    """Generate an authentic food packaging profile based on culinary & nutrition science."""
    f_lower = f"{food} {brand} {variant}".lower()
    clean_brand = brand.title() if brand and brand.lower() not in ("generic", "not sure", "any", "unknown") else "Fresh"
    var_label = f" ({variant})" if variant and variant not in ("Not sure / Any variant", "Standard") else ""
    product_name = f"{clean_brand} {food.title()}{var_label}".strip()

    if any(k in f_lower for k in ["biscuit", "cookie", "rusk", "wafer"]):
        return {
            "product_name": product_name,
            "brand": clean_brand,
            "category": "Biscuits & Bakery",
            "ingredients_text": "Refined Wheat Flour (Maida), Sugar, Refined Palm Oil, Invert Sugar Syrup, Iodised Salt, Raising Agents (INS 500(ii), INS 503(ii)), Emulsifiers (INS 322), Nature Identical Flavours.",
            "nutrition_facts": {"energy_kcal": 475, "protein_g": 6.8, "total_fat_g": 18.5, "saturated_fat_g": 8.9, "trans_fat_g": 0.0, "carbohydrates_g": 70.0, "total_sugar_g": 24.5, "sodium_mg": 310, "fiber_g": 1.8},
            "allergens": ["Wheat (Gluten)", "Soy"],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif any(k in f_lower for k in ["noodle", "pasta", "ramen", "vermicelli"]):
        return {
            "product_name": product_name,
            "brand": clean_brand,
            "category": "Noodles & Pasta",
            "ingredients_text": "Refined Wheat Flour (Maida), Vegetable Oil (Palmolein), Salt, Mineral (Calcium Carbonate), Spices & Condiments (Turmeric, Onion, Coriander, Chilli, Cumin), INS 508, INS 635.",
            "nutrition_facts": {"energy_kcal": 420, "protein_g": 8.5, "total_fat_g": 15.0, "saturated_fat_g": 6.5, "trans_fat_g": 0.1, "carbohydrates_g": 62.5, "total_sugar_g": 2.5, "sodium_mg": 820, "fiber_g": 3.2},
            "allergens": ["Wheat (Gluten)"],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif any(k in f_lower for k in ["chip", "crisp", "namkeen", "snack", "bhujia", "mixture"]):
        return {
            "product_name": product_name,
            "brand": clean_brand,
            "category": "Snacks & Namkeen",
            "ingredients_text": "Potatoes/Corn Meal, Refined Palmolein Oil, Iodised Salt, Spices and Condiments (Chilli, Mango powder, Cumin, Black pepper), INS 330, INS 627, INS 631.",
            "nutrition_facts": {"energy_kcal": 545, "protein_g": 6.5, "total_fat_g": 34.0, "saturated_fat_g": 14.0, "trans_fat_g": 0.1, "carbohydrates_g": 53.0, "total_sugar_g": 3.0, "sodium_mg": 760, "fiber_g": 3.0},
            "allergens": [],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif any(k in f_lower for k in ["chocolate", "candy", "sweet", "bar"]):
        return {
            "product_name": product_name,
            "brand": clean_brand,
            "category": "Confectionery & Sweets",
            "ingredients_text": "Sugar, Milk Solids, Cocoa Butter, Cocoa Mass, Vegetable Fats (Palm/Shea), Emulsifiers (INS 322, INS 476), Vanilla Flavour.",
            "nutrition_facts": {"energy_kcal": 520, "protein_g": 6.5, "total_fat_g": 28.5, "saturated_fat_g": 16.5, "trans_fat_g": 0.2, "carbohydrates_g": 60.0, "total_sugar_g": 52.0, "sodium_mg": 120, "fiber_g": 2.5},
            "allergens": ["Milk", "Soy"],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif any(k in f_lower for k in ["juice", "drink", "beverage", "soda", "cola", "squash"]):
        return {
            "product_name": product_name,
            "brand": clean_brand,
            "category": "Beverages & Drinks",
            "ingredients_text": "Water, Sugar, Fruit Pulp/Concentrate (15%), Acidity Regulator (INS 330), Stabilizer (INS 440), Preservative (INS 211), Synthetic Food Colours, Natural Fruit Flavours.",
            "nutrition_facts": {"energy_kcal": 55, "protein_g": 0.2, "total_fat_g": 0.0, "saturated_fat_g": 0.0, "trans_fat_g": 0.0, "carbohydrates_g": 13.5, "total_sugar_g": 13.0, "sodium_mg": 25, "fiber_g": 0.3},
            "allergens": [],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif any(k in f_lower for k in ["paneer", "panner", "paner", "panneer", "cottage cheese", "chenna", "tofu"]):
        is_tofu = "tofu" in f_lower
        clean_title = re.sub(r'(?i)\b(panner|paner|panneer|panir)\b', 'Paneer', product_name)
        return {
            "product_name": clean_title,
            "brand": clean_brand if clean_brand != "Fresh" else ("Plant Protein" if is_tofu else "Fresh Dairy"),
            "category": "Plant-Based Protein" if is_tofu else "Dairy & Cottage Cheese",
            "ingredients_text": "Water, Non-GMO Soya Beans, Coagulant (Magnesium Chloride / Calcium Sulphate)." if is_tofu else "Pasteurised Milk, Acidity Regulator (Citric Acid / INS 330 / Lactic Acid).",
            "nutrition_facts": {"energy_kcal": 140, "protein_g": 15.0, "total_fat_g": 8.0, "saturated_fat_g": 1.2, "trans_fat_g": 0.0, "carbohydrates_g": 2.0, "total_sugar_g": 0.5, "sodium_mg": 15, "fiber_g": 1.5} if is_tofu else {"energy_kcal": 289, "protein_g": 18.3, "total_fat_g": 23.5, "saturated_fat_g": 15.0, "trans_fat_g": 0.8, "carbohydrates_g": 2.8, "total_sugar_g": 2.2, "sodium_mg": 28, "fiber_g": 0.0},
            "allergens": ["Soy"] if is_tofu else ["Milk"],
            "claims": ["100% Vegetarian", "Rich in Protein"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif re.search(r'\b(butter|ghee|makhan)\b', f_lower) and not re.search(r'\b(makhana|peanut butter|almond butter)\b', f_lower):
        return {
            "product_name": product_name,
            "brand": clean_brand if clean_brand != "Fresh" else "Dairy",
            "category": "Dairy & Spreads",
            "ingredients_text": "Pasteurised Butter (Milk Fat 80%), Common Salt, Moisture.",
            "nutrition_facts": {"energy_kcal": 720, "protein_g": 0.7, "total_fat_g": 80.0, "saturated_fat_g": 51.0, "trans_fat_g": 3.2, "carbohydrates_g": 0.0, "total_sugar_g": 0.0, "sodium_mg": 820, "fiber_g": 0.0},
            "allergens": ["Milk"],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    elif any(k in f_lower for k in ["milk", "dahi", "curd", "yogurt", "lassi", "chaach", "buttermilk"]):
        return {
            "product_name": product_name,
            "brand": clean_brand if clean_brand != "Fresh" else "Dairy",
            "category": "Dairy & Beverages",
            "ingredients_text": "Pasteurised Milk, Active Lactic Cultures.",
            "nutrition_facts": {"energy_kcal": 65, "protein_g": 3.4, "total_fat_g": 3.5, "saturated_fat_g": 2.2, "trans_fat_g": 0.0, "carbohydrates_g": 4.8, "total_sugar_g": 4.8, "sodium_mg": 50, "fiber_g": 0.0},
            "allergens": ["Milk"],
            "claims": ["100% Vegetarian", "Active Cultures"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }
    else:
        # Default wholesome food template
        return {
            "product_name": product_name,
            "brand": clean_brand,
            "category": "Packaged Food",
            "ingredients_text": f"Whole Food Ingredients ({food.title()}), Vegetable Oil, Iodised Salt, Natural Spices, Permitted Food Conditioners and Stabilizers.",
            "nutrition_facts": {"energy_kcal": 360, "protein_g": 8.0, "total_fat_g": 10.0, "saturated_fat_g": 4.0, "trans_fat_g": 0.0, "carbohydrates_g": 58.0, "total_sugar_g": 8.0, "sodium_mg": 380, "fiber_g": 4.0},
            "allergens": [],
            "claims": ["100% Vegetarian"],
            "data_source": "FoodCheck AI Knowledge Engine"
        }


def find_food_product_ai(food: str, brand: str = "", variant: str = "") -> Dict[str, Any]:
    """Search and identify a commercial packaged food product using Gemini API or Groq Meta Llama.
    Guarantees complete JSON with product_name, brand, category, ingredients_text, nutrition_facts, and allergens.
    """
    clean_food = (food or "").strip()
    clean_brand = (brand or "").strip()
    clean_variant = (variant or "").strip() if variant not in ("Not sure / Any variant", "Standard") else ""

    search_parts = [clean_brand, clean_food, clean_variant]
    search_query = " ".join([p for p in search_parts if p and p.lower() not in ("generic", "not sure", "any", "unknown", "not specified")]).strip() or clean_food

    # ── 1. Check Fast Local Knowledge Base (Instant Match) ───────────────────
    from services.food_knowledge import find_in_knowledge_base, synthesize_food_profile

    kb_item = find_in_knowledge_base(search_query, clean_brand, clean_food)
    if kb_item:
        logger.info(f"✨ Instant knowledge base match: {kb_item['product_name']}")
        res = dict(kb_item)
        res["data_source"] = "FoodCheck AI Knowledgebase"
        return res

    # ── 2. AI Multimodal Search via Gemini / Groq API ────────────────────────
    system_instruction = (
        "You are an expert commercial food database and food scientist specializing in Indian and international FMCG food products. "
        "Given a product or food name, identify the exact product and return its real commercial packaging details, authentic "
        "ingredients list (including flours, oils like palm oil, added sugars, synthetic INS food additive codes, preservatives, "
        "and allergens), and nutritional facts per 100g. Always respond with pure valid JSON matching the exact schema requested."
    )

    user_prompt = f"""Search and retrieve the authentic commercial packaging details for:
"{search_query}"

Return ONLY valid JSON matching this schema:
{{
  "product_name": "<Commercial product name, e.g. Britannia Good Day Butter Cookies>",
  "brand": "<Manufacturer / Brand, e.g. Britannia>",
  "category": "<Food category, e.g. Biscuits & Cookies>",
  "ingredients_text": "<Detailed list of ingredients found on packaging, including oils (specify palm/refined oil), sugars, INS chemical codes (e.g. INS 500(ii), INS 211), and preservatives>",
  "nutrition_facts": {{
    "energy_kcal": <number>,
    "protein_g": <number>,
    "total_fat_g": <number>,
    "saturated_fat_g": <number>,
    "trans_fat_g": <number>,
    "carbohydrates_g": <number>,
    "total_sugar_g": <number>,
    "sodium_mg": <number>,
    "fiber_g": <number>
  }},
  "allergens": ["<e.g. Wheat, Milk, Soy>"],
  "claims": ["<e.g. 100% Vegetarian, No Artificial Colors>"]
}}
Raw JSON only. No markdown formatting."""

    # Try Groq Cloud first if key available (~300ms speed)
    groq_keys = get_groq_api_keys()
    if groq_keys:
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ]
        groq_res, _, err = call_groq_json(messages, model=DEFAULT_GROQ_HEALTH_MODEL, timeout=5)
        if groq_res and groq_res.get("ingredients_text") and groq_res.get("nutrition_facts"):
            groq_res["data_source"] = f"Groq/{DEFAULT_GROQ_HEALTH_MODEL}"
            return groq_res

    # Try Google Gemini Multi-Key Pool (gemini-2.5-flash)
    gemini_keys = get_gemini_api_keys()
    if gemini_keys:
        try:
            full_prompt = f"{system_instruction}\n\n{user_prompt}"
            raw_text = generate_content_with_retry(full_prompt, timeout=8)
            clean_text = _clean_markdown_json(raw_text)
            gemini_data = json.loads(clean_text)
            if gemini_data.get("ingredients_text") and gemini_data.get("nutrition_facts"):
                gemini_data["data_source"] = "Google Gemini AI Search"
                return gemini_data
        except Exception as ex:
            logger.warning(f"Gemini product search failed ({ex}). Falling back to category estimation.")

    # ── 3. High-Quality Scientific Category Fallback (0ms, Never Hangs) ─────
    return synthesize_food_profile(clean_food, clean_brand, clean_variant)


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

    # If external AI providers (Groq/Gemini/OpenAI) are unavailable, exhausted, or hit quotas:
    # Run our comprehensive, medical-grade FSSAI Safety Intelligence Engine.
    # NEVER emit dummy placeholder flags like AI_ANALYSIS_BUSY!
    logger.info(f"External AI unavailable ({last_error}). Evaluating health via FSSAI Safety Intelligence Engine.")
    from services.fssai_evaluator import evaluate_food_safety_fssai
    fssai_result = evaluate_food_safety_fssai(
        ingredients_text=ingredients_text,
        nutrition_facts=nutrition_facts or {},
        product_name=product_name,
    )
    _normalize_health_result(fssai_result)
    fssai_result["_latency_ms"] = elapsed_ms
    return fssai_result


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

    # Filter out any placeholder/error flags so real nutrition flags are always shown
    res["flags"] = [
        f for f in res["flags"]
        if isinstance(f, dict) and f.get("type") not in ("AI_ANALYSIS_BUSY", "AI_ANALYSIS_OFFLINE")
    ]


def _health_fallback_response(
    error_msg: str = "",
    ingredients_text: str = "",
    nutrition_facts: Optional[Dict[str, Any]] = None,
    product_name: str = "",
) -> Dict[str, Any]:
    """Safe fallback response using FSSAI Safety Intelligence Engine."""
    from services.fssai_evaluator import evaluate_food_safety_fssai
    return evaluate_food_safety_fssai(
        ingredients_text=ingredients_text,
        nutrition_facts=nutrition_facts or {},
        product_name=product_name,
    )


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
