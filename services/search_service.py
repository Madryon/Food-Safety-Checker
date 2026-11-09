"""
search_service.py – Open Food Facts autocomplete and AI analysis for the
guided "Search Food Safety" wizard.

Provides:
  get_food_suggestions(q)          → list[str]
  get_brand_suggestions(q, food)   → list[str]
  get_variant_suggestions(brand, food) → list[str]
  get_search_report(food, brand, variant) → dict  (same schema as photo scan)
"""
import time
import json
import logging
import threading
import requests
from typing import List, Optional, Dict, Any

from services.search_guard import FOOD_CATEGORIES, KNOWN_BRANDS

logger = logging.getLogger(__name__)

# ── OFF config ───────────────────────────────────────────────────────────────
_OFF_BASE = "https://world.openfoodfacts.org/api/v2/search"
_OFF_HEADERS = {"User-Agent": "FoodCheck-India - WebApp - Version 1.0"}
_TIMEOUT = 5

# ── Simple 60-second suggestion cache ────────────────────────────────────────
_SUGGEST_CACHE: Dict[str, Any] = {}
_SUGGEST_LOCK = threading.Lock()
_SUGGEST_TTL = 60


def _sget(key: str):
    with _SUGGEST_LOCK:
        entry = _SUGGEST_CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _SUGGEST_TTL:
            return entry[1]
        _SUGGEST_CACHE.pop(key, None)
        return None


def _sset(key: str, value):
    with _SUGGEST_LOCK:
        _SUGGEST_CACHE[key] = (time.monotonic(), value)
        if len(_SUGGEST_CACHE) > 500:
            oldest = sorted(_SUGGEST_CACHE, key=lambda k: _SUGGEST_CACHE[k][0])[:100]
            for k in oldest:
                _SUGGEST_CACHE.pop(k, None)


# ── Food Suggestions ─────────────────────────────────────────────────────────

def get_food_suggestions(q: str, limit: int = 8) -> List[str]:
    """Return food category suggestions matching q."""
    q_lower = q.lower().strip()
    if not q_lower:
        return FOOD_CATEGORIES[:limit]

    # Local list first
    local = [
        cat.title() for cat in FOOD_CATEGORIES
        if q_lower in cat or cat.startswith(q_lower)
    ][:limit]

    if len(local) >= limit:
        return local

    # Augment from OFF
    cache_key = f"food_suggest:{q_lower}"
    cached = _sget(cache_key)
    if cached is not None:
        return cached

    off_items: List[str] = []
    try:
        resp = requests.get(
            _OFF_BASE,
            params={
                "search_terms": q,
                "fields": "categories",
                "page_size": 20,
                "json": 1,
            },
            headers=_OFF_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            seen: set = set()
            for product in data.get("products", []):
                cats = product.get("categories", "")
                if cats:
                    for cat in cats.split(","):
                        cat = cat.strip().title()
                        if (
                            q_lower in cat.lower()
                            and 3 < len(cat) < 40
                            and cat not in seen
                        ):
                            seen.add(cat)
                            off_items.append(cat)
                            if len(off_items) >= limit:
                                break
                if len(off_items) >= limit:
                    break
    except Exception as e:
        logger.debug(f"OFF food suggest failed: {e}")

    combined = list(dict.fromkeys(local + off_items))[:limit]
    _sset(cache_key, combined)
    return combined


# ── Brand Suggestions ────────────────────────────────────────────────────────

def get_brand_suggestions(q: str, food: str = "", limit: int = 8) -> List[str]:
    """Return brand suggestions matching q, optionally filtered to food category."""
    q_lower = q.lower().strip()
    if not q_lower:
        return [b for b in KNOWN_BRANDS if len(b) < 20][:limit]

    # Local list first
    local = [
        b for b in KNOWN_BRANDS
        if q_lower in b.lower() or b.lower().startswith(q_lower)
    ][:limit]

    if len(local) >= limit:
        return local

    cache_key = f"brand_suggest:{q_lower}:{food.lower()}"
    cached = _sget(cache_key)
    if cached is not None:
        return cached

    off_brands: List[str] = []
    try:
        params: Dict[str, Any] = {
            "search_terms": f"{q} {food}".strip() if food else q,
            "fields": "brands",
            "page_size": 20,
            "json": 1,
        }
        resp = requests.get(
            _OFF_BASE, params=params, headers=_OFF_HEADERS, timeout=_TIMEOUT
        )
        if resp.status_code == 200:
            data = resp.json()
            seen: set = set()
            for product in data.get("products", []):
                brand_str = product.get("brands", "")
                if brand_str:
                    for b in brand_str.split(","):
                        b = b.strip().title()
                        if (
                            q_lower in b.lower()
                            and 2 < len(b) < 40
                            and b not in seen
                        ):
                            seen.add(b)
                            off_brands.append(b)
                            if len(off_brands) >= limit:
                                break
                if len(off_brands) >= limit:
                    break
    except Exception as e:
        logger.debug(f"OFF brand suggest failed: {e}")

    combined = list(dict.fromkeys(local + off_brands))[:limit]
    _sset(cache_key, combined)
    return combined


# ── Variant Suggestions ──────────────────────────────────────────────────────

def get_variant_suggestions(brand: str, food: str, q: str = "", limit: int = 8) -> List[str]:
    """Return product variants from OFF for a given brand + food."""
    if not brand:
        return []

    cache_key = f"variant_suggest:{brand.lower()}:{food.lower()}:{q.lower()}"
    cached = _sget(cache_key)
    if cached is not None:
        return cached

    variants: List[str] = []
    try:
        params: Dict[str, Any] = {
            "brands": brand,
            "fields": "product_name",
            "page_size": 30,
            "json": 1,
        }
        if food:
            params["search_terms"] = food
        resp = requests.get(
            _OFF_BASE, params=params, headers=_OFF_HEADERS, timeout=_TIMEOUT
        )
        if resp.status_code == 200:
            data = resp.json()
            seen: set = set()
            for product in data.get("products", []):
                name = (product.get("product_name") or "").strip()
                if name and name not in seen:
                    if not q or q.lower() in name.lower():
                        seen.add(name)
                        variants.append(name)
                        if len(variants) >= limit:
                            break
    except Exception as e:
        logger.debug(f"OFF variant suggest failed: {e}")

    # Always offer "Not sure / Any variant" if no results
    if not variants:
        variants = ["Not sure / Any variant"]
    else:
        variants.append("Not sure / Any variant")

    result = variants[:limit]
    _sset(cache_key, result)
    return result


# ── Full Search Report ───────────────────────────────────────────────────────

def get_search_report(food: str, brand: str, variant: str) -> Dict[str, Any]:
    """
    Build a food safety report from text search (same schema as photo scan).
    
    Returns unified dict with keys:
      vision: {product_name, brand, ingredients_text, nutrition_facts, off_match, data_source}
      health: {score, verdict, flags, reasoning, alternatives, _ai_provider, _latency_ms}
      news:   {articles: [...]}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from services.product_lookup import search as off_search
    from services.health_analyzer import analyze as health_analyze
    from services.news_fetcher import get_news

    product_query = variant if (variant and variant != "Not sure / Any variant") else f"{brand} {food}"
    
    # ── Step 1: Parallel OFF lookup + news ───────────────────────────────────
    off_result: Optional[Dict] = None
    news_articles: List = []

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="search_io") as pool:
        fut_off = pool.submit(off_search, product_query, brand)
        fut_news = pool.submit(_safe_news, brand)

        try:
            off_result = fut_off.result(timeout=8)
        except Exception as e:
            logger.warning(f"OFF lookup failed for search: {e}")
            off_result = None

        try:
            news_articles = fut_news.result(timeout=6) or []
        except Exception as e:
            logger.warning(f"News fetch failed for search: {e}")
            news_articles = []

    # ── Step 2: Build vision-like payload from OFF data ──────────────────────
    if off_result:
        ingredients_text = _build_ingredients_text(off_result, food, brand, variant)
        nutrition_facts = off_result.get("nutriments", {})
        product_name = off_result.get("product_name") or f"{brand} {food}".strip()
        actual_brand = off_result.get("brands") or brand
        data_source = "Open Food Facts"
    else:
        # No OFF match — use search terms + AI estimation
        ingredients_text = (
            f"Product: {brand} {variant if variant != 'Not sure / Any variant' else food}. "
            f"Category: {food}. Brand: {brand}. "
            "Note: Ingredients not found in database — estimated from product category."
        )
        nutrition_facts = {}
        product_name = f"{brand} {food}".strip()
        actual_brand = brand
        data_source = "AI Estimation (not in database)"

    vision_data = {
        "product_name": product_name,
        "brand": actual_brand,
        "ingredients_text": ingredients_text,
        "nutrition_facts": nutrition_facts,
        "off_match": off_result,
        "data_source": data_source,
    }

    # ── Step 3: Health analysis ───────────────────────────────────────────────
    try:
        health_data = health_analyze(ingredients_text, nutrition_facts, product_name)
    except Exception as e:
        logger.error(f"Health analysis failed in search: {e}")
        health_data = {
            "score": None,
            "verdict": "unknown",
            "flags": [],
            "alternatives": [],
            "reasoning": f"Health analysis failed: {str(e)}",
        }

    return {
        "vision": vision_data,
        "health": health_data,
        "news": {"articles": news_articles},
    }


def _safe_news(brand: str) -> List:
    try:
        from services.news_fetcher import get_news
        return get_news(brand) or []
    except Exception:
        return []


def _build_ingredients_text(off: Dict, food: str, brand: str, variant: str) -> str:
    """Build a descriptive ingredients string from OFF product data."""
    parts = []

    # Actual ingredients from OFF
    # OFF stores ingredients in ingredients_text_en or ingredients_text
    ing = off.get("ingredients_text") or off.get("ingredients_text_en") or ""
    if ing:
        parts.append(f"Ingredients: {ing}")

    # Allergens
    allergens = off.get("allergens", [])
    if allergens:
        if isinstance(allergens, list):
            allergen_str = ", ".join(str(a).replace("en:", "").replace("_", " ") for a in allergens)
        else:
            allergen_str = str(allergens)
        parts.append(f"Allergens: {allergen_str}")

    # Categories
    cats = off.get("categories", "")
    if cats:
        parts.append(f"Category: {cats[:200]}")

    # Nova group
    nova = off.get("nova_group")
    if nova:
        parts.append(f"NOVA Group: {nova} (processing level)")

    # Nutriscore
    nutriscore = off.get("nutriscore_grade")
    if nutriscore:
        parts.append(f"Nutri-Score: {str(nutriscore).upper()}")

    if not parts:
        # Fallback
        parts.append(
            f"Product: {brand} {variant if variant != 'Not sure / Any variant' else food}. "
            f"Category: {food}. Brand: {brand}. "
            "No detailed ingredient list found in database."
        )

    return "\n".join(parts)
