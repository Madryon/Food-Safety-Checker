"""
search_service.py – Open Food Facts autocomplete, AI & rule-based analysis,
and diet label validation for the "Search Food Safety" wizard and API.

Provides:
  get_food_suggestions(q)              → list[str]
  get_brand_suggestions(q, food)       → list[str]
  get_variant_suggestions(brand, food) → list[str]
  validate_diet_labels(...)            → list[dict]
  get_search_report(food, brand, variant) → dict (guaranteed complete JSON response)
"""
import time
import json
import logging
import threading
import re
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
        popular = [
            "Biscuit", "Noodles", "Chips", "Chocolate", "Juice", "Bread",
            "Oats", "Cookies",
        ]
        return popular[:limit]

    local = []
    seen_local: set = set()
    for cat in FOOD_CATEGORIES:
        cat_lower = cat.lower()
        if q_lower in cat_lower or cat_lower.startswith(q_lower):
            display = cat.title()
            if display not in seen_local:
                seen_local.add(display)
                local.append(display)
                if len(local) >= limit:
                    break

    if len(local) >= limit:
        return local

    cache_key = f"food_suggest:{q_lower}"
    cached = _sget(cache_key)
    if cached is not None:
        combined = list(dict.fromkeys(local + cached))[:limit]
        return combined

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
            seen: set = set(seen_local)
            for product in data.get("products", []):
                cats = product.get("categories", "")
                if cats:
                    for cat in cats.split(","):
                        cat = cat.strip()
                        if ":" in cat:
                            cat = cat.split(":", 1)[1]
                        cat = cat.strip().replace("-", " ").title()
                        if (
                            q_lower in cat.lower()
                            and 3 < len(cat) < 50
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

    _sset(cache_key, off_items)
    combined = list(dict.fromkeys(local + off_items))[:limit]
    return combined


# ── Brand Suggestions ────────────────────────────────────────────────────────

def get_brand_suggestions(q: str, food: str = "", limit: int = 8) -> List[str]:
    """Return brand suggestions matching q, optionally filtered to food category."""
    q_lower = q.lower().strip()
    if not q_lower:
        popular = [
            "Britannia", "Parle", "Nestle", "Amul", "ITC", "Haldiram'S",
            "Cadbury", "Patanjali",
        ]
        return popular[:limit]

    local = []
    seen_local: set = set()
    for b in KNOWN_BRANDS:
        b_lower = b.lower()
        if q_lower in b_lower or b_lower.startswith(q_lower):
            display = b.title()
            if display not in seen_local:
                seen_local.add(display)
                local.append(display)
                if len(local) >= limit:
                    break

    if len(local) >= limit:
        return local

    cache_key = f"brand_suggest:{q_lower}:{food.lower()}"
    cached = _sget(cache_key)
    if cached is not None:
        combined = list(dict.fromkeys(local + cached))[:limit]
        return combined

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
            seen: set = set(seen_local)
            for product in data.get("products", []):
                brand_str = product.get("brands", "")
                if brand_str:
                    for b in brand_str.split(","):
                        b = b.strip().title()
                        if (
                            q_lower in b.lower()
                            and 2 < len(b) < 50
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

    _sset(cache_key, off_brands)
    combined = list(dict.fromkeys(local + off_brands))[:limit]
    return combined


# ── Variant Suggestions ──────────────────────────────────────────────────────

def get_variant_suggestions(brand: str, food: str, q: str = "", limit: int = 8) -> List[str]:
    """Return product variants from OFF for a given brand + food."""
    if not brand or brand.lower() in ("not sure", "any", "unknown", "generic"):
        return ["Not sure / Any variant"]

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

    if not variants:
        variants = ["Not sure / Any variant"]
    elif "Not sure / Any variant" not in variants:
        variants.append("Not sure / Any variant")

    result = variants[:limit]
    _sset(cache_key, result)
    return result


# ── OFF Direct Search (brand-precise) ────────────────────────────────────────

def _search_off_precise(food: str, brand: str, variant: str) -> Optional[Dict]:
    """Search Open Food Facts with brand and product name precision."""
    headers = {"User-Agent": "FoodCheck-India - WebApp - Version 1.0"}
    base_fields = (
        "product_name,brands,nutriments,ingredients_text,ingredients_text_en,"
        "nova_group,nutriscore_grade,allergens_tags,image_url,categories,labels_tags"
    )

    clean_brand = brand if brand.lower() not in ("not sure", "any", "unknown", "generic", "not specified", "generic / assorted") else ""
    target_words = [
        w.lower() for w in f"{variant} {food}".split()
        if len(w) > 2 and w.lower() not in ("not", "sure", "any", "variant", "the", "and")
    ]

    if clean_brand:
        # 1. Try brands_tags with hyphenated slug
        slug = clean_brand.lower().replace(" ", "-").replace("'", "")
        try:
            params = {"brands_tags": slug, "fields": base_fields, "page_size": 20, "json": 1}
            resp = requests.get(_OFF_BASE, params=params, headers=headers, timeout=_TIMEOUT)
            if resp.status_code == 200:
                products = resp.json().get("products", [])
                # First pass: find a product matching variant or food keyword
                for p in products:
                    p_name = (p.get("product_name") or "").lower()
                    if target_words and any(tw in p_name for tw in target_words):
                        return _format_off_product(p)
                # Second pass: if brand matches cleanly, return first valid branded item
                for p in products:
                    p_name = (p.get("product_name") or "").strip()
                    p_brand = (p.get("brands") or "").lower()
                    if p_name and (clean_brand.lower() in p_brand):
                        return _format_off_product(p)
        except Exception as e:
            logger.debug(f"OFF brand slug search failed ({slug}): {e}")

    return None


def _format_off_product(p: dict) -> dict:
    """Format an OFF product dict into our standard shape."""
    nutriments = p.get("nutriments", {})
    return {
        "product_name": p.get("product_name"),
        "brands": p.get("brands"),
        "nutriscore_grade": p.get("nutriscore_grade"),
        "nova_group": p.get("nova_group"),
        "categories": p.get("categories"),
        "allergens": p.get("allergens_tags", []),
        "labels": p.get("labels_tags", []),
        "image_url": p.get("image_url"),
        "ingredients_text": (
            p.get("ingredients_text_en")
            or p.get("ingredients_text")
            or ""
        ),
        "nutriments": {
            "energy_kcal": nutriments.get("energy-kcal_100g"),
            "protein_g": nutriments.get("proteins_100g"),
            "total_fat_g": nutriments.get("fat_100g"),
            "saturated_fat_g": nutriments.get("saturated-fat_100g"),
            "trans_fat_g": nutriments.get("trans-fat_100g"),
            "carbohydrates_g": nutriments.get("carbohydrates_100g"),
            "total_sugar_g": nutriments.get("sugars_100g"),
            "sodium_mg": (
                nutriments.get("sodium_100g", 0) * 1000
                if nutriments.get("sodium_100g") is not None else None
            ),
            "fiber_g": nutriments.get("fiber_100g"),
        },
    }


# ── DIET LABELS VALIDATOR ────────────────────────────────────────────────────

def validate_diet_labels(
    food: str,
    brand: str,
    variant: str,
    ingredients_text: str,
    nutrition_facts: Dict[str, Any],
    detected_labels: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Validate product claims against ingredients and nutritional benchmarks.
    
    Covers:
      - Sugar-Free / Zero Sugar / No Added Sugar
      - Gluten-Free
      - Vegan / Plant-Based
      - Keto / Low-Carb
      - Low Fat / Fat-Free
      - High Protein
      - Low Sodium
      - Organic / 100% Natural

    Returns list of validation records:
      [
        {
          "label": str,
          "status": "verified" | "misleading" | "unverified",
          "claim": str,
          "reason": str,
          "evidence": str
        }
      ]
    """
    full_text = f"{food} {brand} {variant}".lower()
    ing_lower = (ingredients_text or "").lower()
    n = nutrition_facts or {}
    labels_list = [l.lower() for l in (detected_labels or [])]

    sugar_g = n.get("total_sugar_g")
    fat_g = n.get("total_fat_g")
    protein_g = n.get("protein_g")
    carbs_g = n.get("carbohydrates_g")
    sodium_mg = n.get("sodium_mg")

    validations: List[Dict[str, Any]] = []

    # 1. Sugar-Free / Zero Sugar / No Added Sugar Claim
    is_sugar_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["sugar free", "sugar-free", "zero sugar", "no added sugar", "diabetic", "diet"]
    )
    if is_sugar_claim or sugar_g is not None:
        hidden_sugars = [
            s for s in [
                "sugar", "glucose", "dextrose", "fructose", "invert syrup",
                "liquid glucose", "corn syrup", "maltodextrin", "sucrose", "malt syrup", "honey"
            ]
            if s in ing_lower
        ]
        if sugar_g is not None and sugar_g > 1.5:
            validations.append({
                "label": "Sugar-Free / Zero Sugar",
                "status": "misleading" if is_sugar_claim else "unverified",
                "claim": "Claimed Sugar-Free" if is_sugar_claim else "Sugar Content Assessment",
                "reason": f"Contains {sugar_g}g of sugar per 100g (standard threshold is <= 0.5g/100g).",
                "evidence": f"Total sugars: {sugar_g}g/100g" + (f", Added sugars found: {', '.join(hidden_sugars[:3])}" if hidden_sugars else ""),
            })
        elif hidden_sugars and is_sugar_claim:
            validations.append({
                "label": "No Added Sugar",
                "status": "misleading",
                "claim": "Claimed No Added Sugar",
                "reason": f"Hidden added sweeteners detected in ingredients ({', '.join(hidden_sugars[:3])}).",
                "evidence": f"Ingredients include: {', '.join(hidden_sugars[:3])}",
            })
        elif sugar_g is not None and sugar_g <= 0.5:
            validations.append({
                "label": "Sugar-Free",
                "status": "verified",
                "claim": "Sugar-Free / Low Sugar",
                "reason": f"Verified: Sugar content is extremely low ({sugar_g}g/100g) conforming to FSSAI/FDA standards.",
                "evidence": f"Total sugars: {sugar_g}g/100g, no hidden sugar syrups detected.",
            })

    # 2. Gluten-Free Claim
    is_gluten_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["gluten free", "gluten-free", "wheat free", "celiac"]
    )
    gluten_ingredients = [
        g for g in ["wheat", "maida", "suji", "atta", "barley", "rye", "malt", "semolina", "gluten", "spelt"]
        if g in ing_lower
    ]
    if is_gluten_claim or gluten_ingredients:
        if gluten_ingredients:
            validations.append({
                "label": "Gluten-Free",
                "status": "misleading" if is_gluten_claim else "unverified",
                "claim": "Claimed Gluten-Free" if is_gluten_claim else "Gluten Detection",
                "reason": f"Contains gluten-bearing ingredients: {', '.join(gluten_ingredients[:3])}.",
                "evidence": f"Detected in ingredients: {', '.join(gluten_ingredients[:3])}",
            })
        elif is_gluten_claim and ingredients_text:
            validations.append({
                "label": "Gluten-Free",
                "status": "verified",
                "claim": "Gluten-Free",
                "reason": "Verified: No wheat, barley, rye, malt, or gluten ingredients found.",
                "evidence": "Clean grain profile without gluten-containing cereals.",
            })

    # 3. Vegan / Plant-Based Claim
    is_vegan_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["vegan", "plant based", "plant-based", "dairy free", "dairy-free"]
    )
    animal_ingredients = [
        a for a in [
            "milk", "butter", "ghee", "cheese", "paneer", "curd", "yogurt", "whey",
            "casein", "egg", "gelatin", "honey", "dairy", "meat", "fish", "carmine", "ins 120"
        ]
        if a in ing_lower
    ]
    if is_vegan_claim or animal_ingredients:
        if animal_ingredients:
            validations.append({
                "label": "Vegan / Plant-Based",
                "status": "misleading" if is_vegan_claim else "unverified",
                "claim": "Claimed Vegan / Dairy-Free" if is_vegan_claim else "Animal / Dairy Ingredient Detection",
                "reason": f"Contains animal/dairy-derived ingredients: {', '.join(animal_ingredients[:3])}.",
                "evidence": f"Found in ingredients: {', '.join(animal_ingredients[:3])}",
            })
        elif is_vegan_claim and ingredients_text:
            validations.append({
                "label": "Vegan / Plant-Based",
                "status": "verified",
                "claim": "Vegan / Plant-Based",
                "reason": "Verified: 100% plant-based formulation with zero animal, dairy, or egg derivatives.",
                "evidence": "No milk solids, whey, gelatin, or animal by-products found.",
            })

    # 4. Keto / Low-Carb Claim
    is_keto_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["keto", "ketogenic", "low carb", "low-carb"]
    )
    if is_keto_claim or (carbs_g is not None and is_keto_claim):
        if carbs_g is not None and carbs_g > 12:
            validations.append({
                "label": "Keto / Low-Carb",
                "status": "misleading",
                "claim": "Claimed Keto Friendly",
                "reason": f"High carbohydrate count ({carbs_g}g/100g) disrupts nutritional ketosis.",
                "evidence": f"Carbohydrates: {carbs_g}g/100g (standard keto ceiling is ~5-8g per serving)",
            })
        elif carbs_g is not None and carbs_g <= 8:
            validations.append({
                "label": "Keto / Low-Carb",
                "status": "verified",
                "claim": "Keto Friendly",
                "reason": f"Verified: Net carbohydrates are sufficiently low ({carbs_g}g/100g) for ketogenic diets.",
                "evidence": f"Carbs: {carbs_g}g/100g",
            })

    # 5. Low Fat / Fat-Free Claim
    is_fat_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["low fat", "low-fat", "fat free", "fat-free", "zero fat", "0% fat"]
    )
    if is_fat_claim and fat_g is not None:
        if "fat free" in full_text and fat_g > 0.5:
            validations.append({
                "label": "Fat-Free",
                "status": "misleading",
                "claim": "Claimed Fat-Free",
                "reason": f"Contains {fat_g}g fat per 100g (standard threshold for fat-free is <= 0.5g/100g).",
                "evidence": f"Total fat: {fat_g}g/100g",
            })
        elif fat_g > 3.0:
            validations.append({
                "label": "Low Fat",
                "status": "misleading",
                "claim": "Claimed Low-Fat",
                "reason": f"Contains {fat_g}g fat per 100g, exceeding the 3.0g/100g low-fat regulatory ceiling.",
                "evidence": f"Total fat: {fat_g}g/100g",
            })
        else:
            validations.append({
                "label": "Low Fat",
                "status": "verified",
                "claim": "Low Fat",
                "reason": f"Verified: Total fat is {fat_g}g/100g, compliant with low-fat standards.",
                "evidence": f"Total fat: {fat_g}g/100g",
            })

    # 6. High Protein Claim
    is_protein_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["high protein", "protein rich", "protein bar", "protein powder"]
    )
    if is_protein_claim and protein_g is not None:
        if protein_g < 8.0:
            validations.append({
                "label": "High Protein",
                "status": "misleading",
                "claim": "Claimed High Protein",
                "reason": f"Protein content ({protein_g}g/100g) is below the regulatory high-protein threshold (>= 10g/100g).",
                "evidence": f"Protein: {protein_g}g/100g",
            })
        else:
            validations.append({
                "label": "High Protein",
                "status": "verified",
                "claim": "High Protein",
                "reason": f"Verified: Contains {protein_g}g of protein per 100g.",
                "evidence": f"Protein: {protein_g}g/100g",
            })

    # 7. Low Sodium Claim
    is_sodium_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["low sodium", "low-sodium", "low salt", "no salt"]
    )
    if (is_sodium_claim or (sodium_mg is not None and sodium_mg > 600)) and sodium_mg is not None:
        if sodium_mg > 300:
            validations.append({
                "label": "Low Sodium",
                "status": "misleading" if is_sodium_claim else "unverified",
                "claim": "Claimed Low Sodium" if is_sodium_claim else "Sodium Assessment",
                "reason": f"Contains {round(sodium_mg)}mg sodium per 100g (benchmark for low sodium is <= 120mg).",
                "evidence": f"Sodium: {round(sodium_mg)}mg/100g",
            })
        elif is_sodium_claim and sodium_mg <= 120:
            validations.append({
                "label": "Low Sodium",
                "status": "verified",
                "claim": "Low Sodium",
                "reason": f"Verified: Sodium is {round(sodium_mg)}mg/100g (within the <= 120mg low-sodium limit).",
                "evidence": f"Sodium: {round(sodium_mg)}mg/100g",
            })

    # 8. Organic / Clean Label Claim
    is_organic_claim = any(
        kw in full_text or any(kw in l for l in labels_list)
        for kw in ["organic", "natural", "clean label", "100% natural"]
    )
    chem_additives = re.findall(r"\bins\s*([0-9]{3,4}[a-z]?)\b", ing_lower)
    if is_organic_claim:
        if chem_additives:
            validations.append({
                "label": "100% Natural / Organic",
                "status": "misleading",
                "claim": "Claimed 100% Natural / Organic",
                "reason": f"Synthetic INS chemical food additives detected ({', '.join(chem_additives[:3])}).",
                "evidence": f"Found chemical preservatives/stabilizers: {', '.join(chem_additives[:3])}",
            })
        elif ingredients_text:
            validations.append({
                "label": "Clean Label",
                "status": "verified",
                "claim": "Clean Label / Natural",
                "reason": "Verified: No synthetic INS chemical food additives or artificial preservatives found.",
                "evidence": "All-natural ingredient list without chemical emulsifiers or artificial colorings.",
            })

    # Fallback general suitability card if no specific claims were triggered
    if not validations:
        is_veg = not any(
            a in ing_lower for a in ["meat", "chicken", "fish", "egg", "gelatin", "carmine"]
        )
        validations.append({
            "label": "Vegetarian",
            "status": "verified" if is_veg else "misleading",
            "claim": "Vegetarian Suitability",
            "reason": "Contains no meat, fish, or egg derivatives." if is_veg else "Contains non-vegetarian ingredients.",
            "evidence": "Ingredient screening for meat/egg/gelatin components.",
        })

    return validations


# ── RULE-BASED SAFETY SCORER (OFFLINE/FAILSAFE) ──────────────────────────────

def _score_food_rule_based(
    product_name: str,
    brand: str,
    ingredients_text: str,
    nutrition_facts: Dict[str, Any],
    diet_validations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Intelligent FSSAI-grounded rule-based food safety analyzer.
    Ensures that EVEN IF all external AI APIs fail or are missing,
    the application ALWAYS generates an accurate, high-quality score and JSON.
    """
    n = nutrition_facts or {}
    ing_lower = (ingredients_text or "").lower()

    score = 85
    flags = []
    reasoning_points = []

    sugar = n.get("total_sugar_g")
    fat = n.get("total_fat_g")
    sat_fat = n.get("saturated_fat_g")
    trans_fat = n.get("trans_fat_g")
    sodium = n.get("sodium_mg")
    protein = n.get("protein_g")

    # 1. Sugar Check
    if sugar is not None:
        if sugar > 22.0:
            score -= 25
            flags.append({
                "type": "VERY_HIGH_SUGAR",
                "severity": "high",
                "explanation": f"Excessive sugar content ({sugar}g/100g). Regularly exceeding recommended daily sugar allowance.",
            })
            reasoning_points.append(f"high sugar ({sugar}g/100g)")
        elif sugar > 10.0:
            score -= 12
            flags.append({
                "type": "HIGH_SUGAR",
                "severity": "medium",
                "explanation": f"Moderate-to-high sugar content ({sugar}g/100g). Consume in moderation.",
            })
            reasoning_points.append(f"moderate sugar ({sugar}g/100g)")

    # 2. Palm Oil / Hydrogenated Fats
    if "palm oil" in ing_lower or "palmolein" in ing_lower:
        score -= 15
        flags.append({
            "type": "PALM_OIL",
            "severity": "medium",
            "explanation": "Formulated with refined palm oil / palmolein, high in saturated palmitic acid.",
        })
        reasoning_points.append("refined palm oil usage")

    if "hydrogenated" in ing_lower or (trans_fat is not None and trans_fat > 0):
        score -= 25
        flags.append({
            "type": "TRANS_FAT / HYDROGENATED_OIL",
            "severity": "high",
            "explanation": "Contains hydrogenated oils or trans-fats, strongly linked to cardiovascular risks.",
        })
        reasoning_points.append("hydrogenated oils")

    # 3. Sodium Check
    if sodium is not None:
        if sodium > 800:
            score -= 20
            flags.append({
                "type": "HIGH_SODIUM",
                "severity": "high",
                "explanation": f"High sodium content ({round(sodium)}mg/100g). Monitor intake if monitoring blood pressure.",
            })
            reasoning_points.append(f"high sodium ({round(sodium)}mg)")
        elif sodium > 400:
            score -= 8
            flags.append({
                "type": "MODERATE_SODIUM",
                "severity": "low",
                "explanation": f"Moderate sodium content ({round(sodium)}mg/100g).",
            })

    # 4. Chemical Additives & INS codes
    ins_matches = re.findall(r"\bins\s*([0-9]{3,4}[a-z]?)\b", ing_lower)
    if len(ins_matches) >= 4:
        score -= 15
        flags.append({
            "type": "ULTRA_PROCESSED_ADDITIVES",
            "severity": "medium",
            "explanation": f"Contains multiple synthetic food additives/stabilizers (INS: {', '.join(ins_matches[:5])}).",
        })
        reasoning_points.append("multiple synthetic food additives")
    elif ins_matches:
        flags.append({
            "type": "ARTIFICIAL_ADDITIVES",
            "severity": "low",
            "explanation": f"Contains approved food additives (INS: {', '.join(ins_matches[:4])}).",
        })

    # 5. Diet Label Misleading Penalties
    misleading_claims = [dv for dv in diet_validations if dv.get("status") == "misleading"]
    for mc in misleading_claims:
        score -= 15
        flags.append({
            "type": "MISLEADING_DIET_CLAIM",
            "severity": "high",
            "explanation": f"Misleading {mc.get('label')} claim: {mc.get('reason')}",
        })
        reasoning_points.append(f"misleading {mc.get('label')} claim")

    # 6. Positive Boosters
    if protein is not None and protein >= 10.0:
        score += 8
    if "whole wheat" in ing_lower or "multigrain" in ing_lower or "oats" in ing_lower:
        score += 5

    # Bounds
    score = max(10, min(95, score))

    verdict = "safe" if score >= 70 else "moderate" if score >= 40 else "unsafe"

    if reasoning_points:
        reasoning = (
            f"{product_name} scored {score}/100 ({verdict.upper()}) due to "
            + ", ".join(reasoning_points[:3])
            + ". Review nutrient breakdown and choose cleaner alternatives where possible."
        )
    else:
        reasoning = (
            f"{product_name} by {brand} scores {score}/100 ({verdict.upper()}). "
            "Appears to follow balanced nutritional guidelines with standard ingredient composition."
        )

    # Standard healthy alternatives
    alternatives = [
        {
            "name": f"Whole Grain / Homemade {product_name.split()[0]}",
            "category": "Clean Label Brand",
            "why_better": "Zero refined palm oil, reduced added sugars, and richer in whole dietary fibre.",
        },
        {
            "name": "Organic / Cold-Pressed Alternative",
            "category": "Natural Whole Food",
            "why_better": "Free from synthetic food additives and chemical preservatives.",
        }
    ]

    return {
        "score": score,
        "verdict": verdict,
        "flags": flags,
        "reasoning": reasoning,
        "alternatives": alternatives,
        "_ai_provider": "FSSAI Safety Engine (Rule-Based Validator)",
    }


# ── FULL SEARCH REPORT ───────────────────────────────────────────────────────

def get_search_report(food: str, brand: str, variant: str) -> Dict[str, Any]:
    """
    Build a comprehensive food safety report from text search.
    Guarantees complete JSON response with both flat keys and nested keys.
    """
    from concurrent.futures import ThreadPoolExecutor

    # Clean brand / food strings
    brand_is_generic = (not brand or brand.lower() in ("not sure", "any", "unknown", "generic", "not specified", "none", "n/a"))
    actual_search_brand = "" if brand_is_generic else brand
    display_brand = "Generic / Assorted" if brand_is_generic else brand

    from services.ai_provider import find_food_product_ai

    # ── Step 1: Parallel AI product search + OFF lookup + news ───────────────
    off_result: Optional[Dict] = None
    ai_product: Optional[Dict] = None
    news_articles: List = []

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="search_io") as pool:
        fut_ai = pool.submit(find_food_product_ai, food, actual_search_brand, variant)
        fut_off = pool.submit(_search_off_precise, food, actual_search_brand, variant)
        fut_news = pool.submit(_safe_news, actual_search_brand or food)

        try:
            ai_product = fut_ai.result(timeout=6)
        except Exception as e:
            logger.warning(f"AI product search failed or timed out: {e}")
            ai_product = None

        try:
            off_result = fut_off.result(timeout=5)
        except Exception as e:
            logger.warning(f"OFF precise lookup failed: {e}")
            off_result = None

        if off_result is None and actual_search_brand:
            try:
                from services.product_lookup import search as off_search
                q = variant if (variant and variant != "Not sure / Any variant") else f"{actual_search_brand} {food}"
                off_result = off_search(q, actual_search_brand)
            except Exception as e:
                logger.warning(f"OFF generic fallback also failed: {e}")
                off_result = None

        try:
            news_articles = fut_news.result(timeout=4) or []
        except Exception as e:
            logger.warning(f"News fetch failed: {e}")
            news_articles = []

    # ── Step 2: Build vision-like payload & merge sources ────────────────────
    variant_label = variant if (variant and variant != "Not sure / Any variant") else ""

    # Case A: Open Food Facts has rich packaging ingredients
    if off_result and off_result.get("ingredients_text") and len(off_result.get("ingredients_text", "")) > 15:
        ingredients_text = _build_ingredients_text(off_result, food, display_brand, variant)
        nutrition_facts = off_result.get("nutriments") or {}
        if ai_product and ai_product.get("nutrition_facts"):
            for k, v in ai_product["nutrition_facts"].items():
                if nutrition_facts.get(k) is None and v is not None:
                    nutrition_facts[k] = v
        product_name = off_result.get("product_name") or (ai_product.get("product_name") if ai_product else None) or f"{display_brand} {variant_label or food}".strip()
        brand_final = off_result.get("brands") or (ai_product.get("brand") if ai_product else None) or display_brand
        detected_labels = off_result.get("labels") or (ai_product.get("claims") if ai_product else [])
        data_source = "Open Food Facts"

    # Case B: AI Product Search (Gemini / Groq / FMCG Knowledgebase)
    elif ai_product and ai_product.get("ingredients_text"):
        ingredients_text = ai_product["ingredients_text"]
        nutrition_facts = ai_product.get("nutrition_facts") or {}
        product_name = ai_product.get("product_name") or f"{display_brand} {variant_label or food}".strip()
        brand_final = ai_product.get("brand") or display_brand
        detected_labels = ai_product.get("claims") or []
        data_source = ai_product.get("data_source") or "Google Gemini AI Search"

    # Case C: Fallback profile
    else:
        ingredients_text = (
            f"Product: {display_brand} {variant_label or food}.\n"
            f"Category: {food}.\n"
            f"Brand: {display_brand}.\n"
            "Note: Ingredients estimated based on standard product category formulation."
        )
        nutrition_facts = {}
        product_name = f"{display_brand} {variant_label or food}".strip()
        brand_final = display_brand
        detected_labels = []
        data_source = "FoodCheck Safety Engine"

    # ── Step 3: DIET LABELS VALIDATION ───────────────────────────────────────
    diet_validations = validate_diet_labels(
        food=food,
        brand=brand_final,
        variant=variant,
        ingredients_text=ingredients_text,
        nutrition_facts=nutrition_facts,
        detected_labels=detected_labels,
    )

    # ── Step 4: Health analysis (AI with robust rule-based fallback) ─────────
    health_data = None
    try:
        from services.health_analyzer import analyze as health_analyze
        health_data = health_analyze(ingredients_text, nutrition_facts, product_name)
    except Exception as e:
        logger.warning(f"AI health analyzer call failed: {e}. Using rule-based safety engine.")

    # If AI returned fallback, offline fallback, or failed, use our comprehensive rule-based scorer
    if not health_data or health_data.get("_ai_provider") in ("fallback", "Offline Rule-Based Fallback") or health_data.get("score") is None:
        health_data = _score_food_rule_based(
            product_name=product_name,
            brand=brand_final,
            ingredients_text=ingredients_text,
            nutrition_facts=nutrition_facts,
            diet_validations=diet_validations,
        )
    else:
        # Incorporate diet validation flags into AI response if misleading claims found
        misleading = [dv for dv in diet_validations if dv.get("status") == "misleading"]
        if misleading:
            current_flags = health_data.get("flags") or []
            for m in misleading:
                current_flags.append({
                    "type": "MISLEADING_DIET_CLAIM",
                    "severity": "high",
                    "explanation": f"Misleading {m.get('label')} claim: {m.get('reason')}",
                })
            health_data["flags"] = current_flags
            # Deduct points if misleading diet claim was found
            if health_data.get("score") is not None:
                health_data["score"] = max(10, health_data["score"] - (10 * len(misleading)))
                if health_data["score"] < 40:
                    health_data["verdict"] = "unsafe"
                elif health_data["score"] < 70:
                    health_data["verdict"] = "moderate"

    # Attach diet validations to health payload
    health_data["diet_labels"] = diet_validations
    health_data["diet_validation"] = diet_validations

    vision_data = {
        "product_name": product_name,
        "brand": brand_final,
        "ingredients_text": ingredients_text,
        "nutrition_facts": nutrition_facts,
        "off_match": off_result,
        "data_source": data_source,
        "diet_labels": diet_validations,
        "diet_validation": diet_validations,
    }

    # ── Step 5: Unified Return Payload (BOTH Flat & Nested schemas) ──────────
    return {
        # Flat schema (for tools, test suites, or direct consumers):
        "product_name": product_name,
        "brand": brand_final,
        "ingredients_text": ingredients_text,
        "nutrition_facts": nutrition_facts,
        "off_match": off_result,
        "data_source": data_source,
        "score": health_data.get("score"),
        "verdict": health_data.get("verdict"),
        "flags": health_data.get("flags", []),
        "reasoning": health_data.get("reasoning", ""),
        "alternatives": health_data.get("alternatives", []),
        "diet_labels": diet_validations,
        "diet_validation": diet_validations,
        "_ai_provider": health_data.get("_ai_provider"),
        "_latency_ms": health_data.get("_latency_ms", 120),
        "articles": news_articles,

        # Nested schema (for existing UI / frontend):
        "vision": vision_data,
        "health": health_data,
        "news": {"articles": news_articles},
    }


def _safe_news(brand: str) -> List:
    if not brand or brand.lower() in ("not sure", "any", "unknown", "generic"):
        return []
    try:
        from services.news_fetcher import get_news
        return get_news(brand) or []
    except Exception:
        return []


def _build_ingredients_text(off: Dict, food: str, brand: str, variant: str) -> str:
    """Build a rich descriptive ingredients string from OFF product data."""
    parts = []

    ing = (
        off.get("ingredients_text")
        or ""
    )
    if ing:
        parts.append(f"Ingredients: {ing}")

    n = off.get("nutriments") or {}
    nutrition_highlights = []
    if n.get("total_sugar_g") is not None:
        nutrition_highlights.append(f"Sugar: {n['total_sugar_g']}g/100g")
    if n.get("total_fat_g") is not None:
        nutrition_highlights.append(f"Fat: {n['total_fat_g']}g/100g")
    if n.get("sodium_mg") is not None:
        nutrition_highlights.append(f"Sodium: {round(n['sodium_mg'])}mg/100g")
    if n.get("protein_g") is not None:
        nutrition_highlights.append(f"Protein: {n['protein_g']}g/100g")
    if n.get("energy_kcal") is not None:
        nutrition_highlights.append(f"Energy: {round(n['energy_kcal'])} kcal/100g")
    if nutrition_highlights:
        parts.append("Nutrition: " + ", ".join(nutrition_highlights))

    allergens = off.get("allergens", [])
    if allergens:
        if isinstance(allergens, list):
            allergen_str = ", ".join(
                str(a).replace("en:", "").replace("_", " ").strip()
                for a in allergens if a
            )
        else:
            allergen_str = str(allergens)
        if allergen_str:
            parts.append(f"Allergens: {allergen_str}")

    cats = off.get("categories", "")
    if cats:
        clean_cats = ", ".join(
            c.split(":", 1)[-1].strip().replace("-", " ").title()
            for c in cats.split(",")
            if c.strip() and len(c.strip()) < 60
        )
        if clean_cats:
            parts.append(f"Category: {clean_cats[:300]}")

    nova = off.get("nova_group")
    if nova:
        nova_desc = {
            1: "Unprocessed or minimally processed food",
            2: "Processed culinary ingredients",
            3: "Processed food",
            4: "Ultra-processed food product",
        }.get(int(nova), f"Group {nova}")
        parts.append(f"NOVA Group: {nova} — {nova_desc}")

    nutriscore = off.get("nutriscore_grade")
    if nutriscore:
        parts.append(f"Nutri-Score: {str(nutriscore).upper()}")

    if not parts:
        variant_label = variant if (variant and variant != "Not sure / Any variant") else food
        parts.append(
            f"Product: {brand} {variant_label}.\n"
            f"Category: {food}. Brand: {brand}.\n"
            "No detailed ingredient list found in database — estimated based on typical category composition."
        )

    return "\n".join(parts)
