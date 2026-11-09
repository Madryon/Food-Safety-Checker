"""
Open Food Facts lookup service with strict relevance validation and robust caching.
Prevents false-positive matches (such as French cheese for Indian paneer or Diet Coke)
and ensures safe, verified photos.
"""

import time
import threading
import re
import requests
from typing import Optional, Dict, Any, List

# In-memory TTL cache for repeated Open Food Facts lookups (60-second TTL)
_OFF_CACHE = {}
_OFF_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60

# Stopwords to ignore when calculating search token overlap
_STOPWORDS = {"the", "and", "for", "with", "pack", "fresh", "best", "any", "not", "sure", "variant", "food", "check", "india", "original"}


def _get_from_cache(key: str):
    with _OFF_CACHE_LOCK:
        entry = _OFF_CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
        _OFF_CACHE.pop(key, None)
        return None


def _set_in_cache(key: str, value):
    with _OFF_CACHE_LOCK:
        _OFF_CACHE[key] = (time.monotonic(), value)
        if len(_OFF_CACHE) > 200:
            oldest = sorted(_OFF_CACHE, key=lambda k: _OFF_CACHE[k][0])[:50]
            for k in oldest:
                _OFF_CACHE.pop(k, None)


def _tokenize(text: str) -> List[str]:
    """Extract lowercased alphanumeric words of length >= 2, skipping stopwords."""
    words = re.findall(r'[a-z0-9]+', (text or "").lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def is_relevant_off_product(product: Dict[str, Any], search_query: str, search_brand: str) -> bool:
    """
    Strict validation to ensure candidate Open Food Facts product genuinely matches
    what the user searched for. Prevents absurd mismatches (e.g. French 'Fromage Blanc'
    for 'Mother Dairy Paneer' or 'Diet Coke').
    """
    if not isinstance(product, dict):
        return False

    p_name = (product.get("product_name") or product.get("product_name_en") or "").lower().strip()
    p_brand = (product.get("brands") or "").lower().strip()

    if not p_name or len(p_name) < 2:
        return False

    q_low = (search_query or "").lower().strip()
    b_low = (search_brand or "").lower().strip()

    # ── 1. Food Type Integrity Guardrails ────────────────────────────────────
    # If the user searched for 'paneer', candidate MUST contain 'paneer' or 'cottage cheese'
    if "paneer" in q_low:
        if "paneer" not in p_name and "cottage cheese" not in p_name:
            return False

    # If the user searched for 'coke' or 'cola', candidate MUST contain 'coke' or 'cola'
    if ("coke" in q_low or "cola" in q_low):
        if "coke" not in p_name and "cola" not in p_name and "coca" not in p_name:
            return False

    # If user searched for 'diet' or 'zero sugar', candidate should not be a sugary regular product
    if ("diet" in q_low or "zero" in q_low) and ("soda" in q_low or "coke" in q_low or "pepsi" in q_low or "drink" in q_low):
        if "diet" not in p_name and "zero" not in p_name and "light" not in p_name and "sans sucre" not in p_name:
            return False

    # If search is for 'butter', candidate must not be biscuits/cakes/namkeen
    if "butter" in q_low and "biscuit" not in q_low and "cookie" not in q_low:
        if "butter" not in p_name and "makhan" not in p_name:
            return False

    # If search is for 'noodles' or 'maggi', candidate must be noodles/pasta
    if ("noodle" in q_low or "maggi" in q_low):
        if "noodle" not in p_name and "maggi" not in p_name and "pasta" not in p_name and "ramen" not in p_name:
            return False

    # If search is for 'chips', candidate must be chips/crisps
    if "chip" in q_low:
        if "chip" not in p_name and "crisp" not in p_name and "wafer" not in p_name:
            return False

    # ── 2. Brand Verification Guard ──────────────────────────────────────────
    if b_low and b_low not in ("not sure", "any", "unknown", "generic"):
        # If user explicitly searched for a brand (e.g. 'Mother Dairy'),
        # candidate MUST have that brand in its brand list or product name!
        if b_low not in p_brand and b_low not in p_name:
            return False

    # ── 3. Token Overlap Check ───────────────────────────────────────────────
    q_tokens = _tokenize(q_low)
    p_tokens = set(_tokenize(f"{p_name} {p_brand}"))

    if not q_tokens:
        return False

    overlap = [t for t in q_tokens if t in p_tokens or any(t in pt for pt in p_tokens)]
    match_ratio = len(overlap) / len(q_tokens)

    # Require at least 50% keyword match or at least 2 strong tokens
    return match_ratio >= 0.5 or len(overlap) >= 2


def search(product_name: str, brand: str) -> Optional[Dict[str, Any]]:
    """
    Query Open Food Facts by product name and brand with 60s TTL caching,
    strict relevance matching, and safe fallback.
    """
    clean_product = (product_name or "").strip()
    clean_brand = (brand or "").strip() if brand and brand.lower() not in ("not sure", "any", "unknown", "generic") else ""
    cache_key = f"{clean_brand.lower()}::{clean_product.lower()}"

    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    full_query = f"{clean_brand} {clean_product}".strip() or clean_product
    if not full_query:
        return None

    headers = {
        "User-Agent": "FoodCheck-India - WebApp - Version 1.0 (https://github.com/Madryon/Food-Safety-Checker)"
    }

    # Search endpoints (prefer India catalog first, fallback to world catalog)
    endpoints = [
        "https://in.openfoodfacts.org/cgi/search.pl",
        "https://world.openfoodfacts.org/cgi/search.pl",
    ]

    queries = [
        full_query,
        clean_product,
    ]

    for base_url in endpoints:
        for q in queries:
            if not q:
                continue

            params = {
                "search_terms": q,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": 10,
                "fields": "product_name,product_name_en,brands,nutriments,ingredients_text,ingredients_text_en,nova_group,"
                          "nutriscore_grade,allergens_tags,image_url,categories,labels_tags",
            }

            try:
                resp = requests.get(base_url, params=params, headers=headers, timeout=3.5)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                products = data.get("products", [])
                if not products:
                    continue

                # Filter and find genuine matches
                for p in products:
                    if is_relevant_off_product(p, full_query, clean_brand):
                        formatted = _format_product(p)
                        _set_in_cache(cache_key, formatted)
                        return formatted

            except (requests.RequestException, ValueError, TimeoutError):
                continue

    # Negative caching for 30s to avoid hammering external service on failed lookups
    _set_in_cache(cache_key, None)
    return None


def _format_product(product: dict) -> dict:
    """Format Open Food Facts product data into standard clean shape."""
    nutriments = product.get("nutriments", {}) or {}
    p_name = product.get("product_name") or product.get("product_name_en") or "Packaged Food"

    return {
        "product_name": p_name,
        "brands": product.get("brands") or "Commercial Brand",
        "nutriscore_grade": product.get("nutriscore_grade"),
        "nova_group": product.get("nova_group"),
        "categories": product.get("categories") or "Packaged Food",
        "allergens": product.get("allergens_tags", []),
        "image_url": product.get("image_url"),
        "ingredients_text": (
            product.get("ingredients_text_en")
            or product.get("ingredients_text")
            or ""
        ),
        "nutriments": {
            "energy_kcal": nutriments.get("energy-kcal_100g") or nutriments.get("energy-kcal"),
            "protein_g": nutriments.get("proteins_100g") or nutriments.get("proteins"),
            "total_fat_g": nutriments.get("fat_100g") or nutriments.get("fat"),
            "saturated_fat_g": nutriments.get("saturated-fat_100g") or nutriments.get("saturated-fat"),
            "trans_fat_g": nutriments.get("trans-fat_100g") or nutriments.get("trans-fat") or 0.0,
            "carbohydrates_g": nutriments.get("carbohydrates_100g") or nutriments.get("carbohydrates"),
            "total_sugar_g": nutriments.get("sugars_100g") or nutriments.get("sugars") or 0.0,
            "sodium_mg": (nutriments.get("sodium_100g", 0) * 1000) if nutriments.get("sodium_100g") is not None else None,
            "fiber_g": nutriments.get("fiber_100g") or nutriments.get("fiber") or 0.0,
        },
    }
