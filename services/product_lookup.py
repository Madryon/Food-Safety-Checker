import requests


def search(product_name: str, brand: str) -> dict | None:
    """Query Open Food Facts by product name and brand.
    
    Returns:
        Matched product data dict or None if no match found.
    """
    base_url = "https://world.openfoodfacts.org/api/v2/search"
    
    # Try combined search first
    queries = [
        f"{brand} {product_name}",
        product_name,
        f"{brand} {product_name.split()[0]}" if product_name else brand,
    ]
    
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
        
        headers = {
            "User-Agent": "FoodCheck-India - WebApp - Version 1.0"
        }
        try:
            resp = requests.get(base_url, params=params, headers=headers, timeout=4)
            resp.raise_for_status()
            data = resp.json()
            
            products = data.get("products", [])
            if not products:
                continue
            
            # Try to find best match
            for product in products:
                p_name = (product.get("product_name") or "").lower()
                p_brand = (product.get("brands") or "").lower()
                
                if (brand.lower() in p_brand or 
                    product_name.lower() in p_name or
                    any(word.lower() in p_name for word in product_name.split() if len(word) > 3)):
                    
                    return _format_product(product)
            
            # Return first result as fallback
            return _format_product(products[0])
        
        except (requests.RequestException, ValueError):
            continue
    
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
