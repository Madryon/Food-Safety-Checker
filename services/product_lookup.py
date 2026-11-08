import time
import threading
import requests

# In-memory TTL cache for repeated Open Food Facts lookups (60-second TTL)
_OFF_CACHE = {}
_OFF_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60


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


def search(product_name: str, brand: str) -> dict | None:
    """Query Open Food Facts by product name and brand with 60s TTL caching and 5s timeout.
    
    Returns:
        Matched product data dict or None if no match found or on timeout/error.
    """
    clean_product = (product_name or "").strip().lower()
    clean_brand = (brand or "").strip().lower()
    cache_key = f"{clean_brand}::{clean_product}"
    
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    base_url = "https://world.openfoodfacts.org/api/v2/search"
    
    # Try combined search first
    queries = [
        f"{brand} {product_name}",
        product_name,
        f"{brand} {product_name.split()[0]}" if product_name else brand,
    ]
    
    headers = {
        "User-Agent": "FoodCheck-India - WebApp - Version 1.0"
    }

    for query in queries:
        if not query or not query.strip():
            continue
        
        params = {
            "search_terms": query,
            "search_simple": 1,
            "action": "process",
            "json": 1,
            "page_size": 5,
            "fields": "product_name,brands,nutriments,ingredients_text,nova_group,"
                      "nutriscore_grade,allergens_tags,image_url,categories",
        }
        
        try:
            resp = requests.get(base_url, params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            
            products = data.get("products", [])
            if not products:
                continue
            
            # Try to find best match
            for product in products:
                p_name = (product.get("product_name") or "").lower()
                p_brand = (product.get("brands") or "").lower()
                
                if (clean_brand in p_brand or 
                    clean_product in p_name or
                    any(word.lower() in p_name for word in clean_product.split() if len(word) > 3)):
                    
                    result = _format_product(product)
                    _set_in_cache(cache_key, result)
                    return result
            
            # Return first result as fallback
            result = _format_product(products[0])
            _set_in_cache(cache_key, result)
            return result
        
        except (requests.RequestException, ValueError, TimeoutError):
            continue
    
    # Negative caching for 30s to avoid hammering external service on failed lookups
    _set_in_cache(cache_key, None)
    return None


def _format_product(product: dict) -> dict:
    """Format Open Food Facts product data into a clean dict."""
    nutriments = product.get("nutriments", {})
    
    return {
        "product_name": product.get("product_name"),
        "brands": product.get("brands"),
        "nutriscore_grade": product.get("nutriscore_grade"),
        "nova_group": product.get("nova_group"),
        "categories": product.get("categories"),
        "allergens": product.get("allergens_tags", []),
        "image_url": product.get("image_url"),
        "nutriments": {
            "energy_kcal": nutriments.get("energy-kcal_100g"),
            "protein_g": nutriments.get("proteins_100g"),
            "total_fat_g": nutriments.get("fat_100g"),
            "saturated_fat_g": nutriments.get("saturated-fat_100g"),
            "trans_fat_g": nutriments.get("trans-fat_100g"),
            "carbohydrates_g": nutriments.get("carbohydrates_100g"),
            "total_sugar_g": nutriments.get("sugars_100g"),
            "sodium_mg": nutriments.get("sodium_100g", 0) * 1000 if nutriments.get("sodium_100g") else None,
            "fiber_g": nutriments.get("fiber_100g"),
        },
    }
