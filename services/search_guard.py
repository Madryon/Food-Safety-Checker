"""
search_guard.py – Input validation and topic guard for Search Food Safety.

5-layer protection:
  1. Field format validation (regex, length)
  2. Injection pattern detection
  3. Open Food Facts relevance check (fast, free)
  4. Gemini binary classification fallback for unknown food/brand
  5. Rate-limit cooldown tracking (per-IP, in-memory)
"""
import re
import time
import json
import logging
import threading
import requests
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# ── Allowed character pattern ───────────────────────────────────────────────
_FIELD_RE = re.compile(r"^[a-zA-Z0-9 ',.\-&()/éèêëàâûùüîïôœç]+$")

# ── Injection / off-topic patterns ──────────────────────────────────────────
_INJECTION_PATTERNS = [
    r"ignore\s+(previous|above|prior|all)\s+(instructions?|prompts?|context)",
    r"you\s+are\s+(now|a)\s+",
    r"act\s+as\s+",
    r"pretend\s+(to\s+)?be\s+",
    r"write\s+(a\s+)?(python|code|program|script|function|class|sql|query)",
    r"tell\s+me\s+(a\s+)?(joke|story|poem|riddle)",
    r"(who|what|where|when|why|how)\s+is\s+the\s+(president|prime\s*minister|ceo|founder|cto)",
    r"\b(select|insert|delete|update|drop|truncate|alter)\s+",
    r"<\s*script",
    r"javascript\s*:",
    r"https?://",
    r"\|\s*(bash|sh|cmd|powershell)",
    r"\$\(",
    r"`[^`]+`",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"system\s*\(",
    r"--\s*(system|prompt|instruction|context)",
    r"\bXSS\b",
    r"<[a-z]+[^>]*>",   # HTML tags
    r"\[.*\]\(http",    # Markdown links
]
_INJECTION_RES = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

# ── Curated food categories (lowercase) ─────────────────────────────────────
FOOD_CATEGORIES = [
    "biscuit", "cookies", "cookie", "noodles", "noodle", "chips", "chocolate",
    "juice", "bread", "rice", "dal", "lentil", "flour", "maida", "suji",
    "besan", "oil", "ghee", "butter", "cheese", "yogurt", "curd", "milk",
    "ice cream", "cereal", "oats", "pasta", "sauce", "pickle", "jam", "honey",
    "tea", "coffee", "soft drink", "energy drink", "aerated drink", "cola",
    "fruit", "vegetable", "snacks", "namkeen", "papad", "wafer", "popcorn",
    "candy", "toffee", "lollipop", "cake", "pastry", "muffin", "roti",
    "paratha", "dosa mix", "idli mix", "upma mix", "soup", "corn flakes",
    "muesli", "granola", "protein bar", "health drink", "basmati rice",
    "wheat flour", "salt", "sugar", "spices", "masala", "condiment", "ketchup",
    "mayonnaise", "vinegar", "soy sauce", "paneer", "tofu", "sprouts",
    "bournvita", "horlicks", "complan", "boost", "milo", "ovaltine",
    "maggi", "top ramen", "yippee", "knorr", "lay's", "kurkure", "doritos",
    "oreo", "bourbon", "marie", "digestive", "cracker", "rusk", "bread stick",
    "peanut butter", "nutella", "spread", "jam", "marmalade", "syrup",
    "protein powder", "whey", "supplement", "vitamin", "mineral water",
    "packaged water", "coconut water", "almond milk", "soy milk",
    "baby food", "infant formula", "cerelac", "biscuit", "snack bar",
    "energy bar", "granola bar", "chocolate bar", "munch", "kitkat",
    "dairy milk", "5 star", "perk", "eclairs", "amul", "kulfi", "lassi",
    "buttermilk", "chhach", "shrikhand", "rasgulla", "gulab jamun", "halwa",
    "ladoo", "barfi", "mithai", "sweets", "dessert", "pudding", "custard",
    "jelly", "gelatin", "agar", "chips masala", "chaat masala", "sambhar",
    "rasam", "curry paste", "biryani mix", "pulao mix", "khichdi mix",
    "ready to eat", "instant food", "frozen food", "canned food", "preserved",
    "pickle", "chutney", "relish", "dip", "hummus", "tahini",
    "breakfast cereal", "muesli bar", "trail mix", "dried fruit",
    "nuts", "almonds", "cashews", "peanuts", "pistachios", "walnuts",
    "seeds", "flaxseeds", "chia seeds", "sunflower seeds",
    "fruit drink", "nectar", "smoothie", "milkshake", "lassi",
    "carbonated drink", "sparkling water", "tonic water",
    "sports drink", "electrolyte", "glucose", "oral rehydration",
    "cooking oil", "sunflower oil", "olive oil", "mustard oil",
    "coconut oil", "palm oil", "soybean oil", "refined oil",
    "ghee", "vanaspati", "margarine", "shortening",
    "sugar free", "diet", "low fat", "low sodium", "organic",
    "natural", "whole grain", "multigrain", "gluten free",
    "vegan", "vegetarian", "jain", "halal",
]

# ── Curated brand list (lowercase for matching) ──────────────────────────────
KNOWN_BRANDS = [
    "britannia", "parle", "sunfeast", "nestle", "itc", "haldiram's", "haldirams",
    "amul", "cadbury", "mondelez", "pepsico", "lay's", "lays", "kurkure",
    "maggi", "bournvita", "horlicks", "munch", "kitkat", "kit kat", "dairy milk",
    "5 star", "perk", "eclairs", "good day", "marie gold", "bourbon",
    "hide & seek", "hide and seek", "krackjack", "monaco", "50-50", "oreo",
    "tiger", "priya gold", "mcvities", "dukes", "anmol", "bikano", "bikaji",
    "jabsons", "garden", "too yumm", "cornitos", "catch", "everest", "mdh",
    "aashirvaad", "patanjali", "dabur", "marico", "fortune", "saffola",
    "nature fresh", "pillsbury", "mtr", "gits", "knorr", "kissan", "heinz",
    "lipton", "brooke bond", "tata tea", "taj mahal", "red label", "sunrise",
    "bru", "nescafe", "complan", "boost", "milo", "ovaltine", "pediasure",
    "ensure", "ensure", "glucon-d", "glucon d", "glucose d", "frooti",
    "real", "paperboat", "appy fizz", "maaza", "minute maid", "tropicana",
    "coke", "coca cola", "pepsi", "7up", "sprite", "fanta", "mirinda",
    "mountain dew", "thums up", "limca", "slice", "nimbooz", "sting",
    "red bull", "monster", "rockstar", "cloud 9", "tzinga",
    "yippee", "top ramen", "ching's", "chings", "mamy poko", "mama earth",
    "mother dairy", "kwality walls", "kwality", "creambell", "havmor",
    "baskin robbins", "naturals", "vadilal", "dinshaw's", "dinshaws",
    "nirma", "surf excel", "tide", "ariel", "dove", "lux", "lifebuoy",
    # (keep only food brands but include FMCG food divisions)
    "mccain", "mcdonald's", "subway", "pizza hut", "dominos", "kfc",
    "burger king", "haldiram", "bikanervala", "bikaner", "raj", "bikaner raj",
    "lijjat", "lijjat papad", "swad", "pushp", "mdh", "everest", "badshah",
    "shaktibhog", "rolly poly", "ganesh", "pran", "druk", "maiyas",
    "id fresh", "bombay halwa", "kohinoor", "india gate", "lal qilla",
    "daawat", "basmati house", "game up", "wingreens", "fabindia",
    "24 mantra", "organic india", "true elements", "yoga bar", "yogabar",
    "ritebite", "max protein", "my fitness", "oziva", "hlth", "kapiva",
    "himalaya", "baidyanath", "zandu", "hamdard", "dabur", "patanjali",
    "himalayan", "nourish organics", "slurrp farm", "early foods",
    "timios", "bagrry's", "bagrrys", "moong dal", "bikaji foods",
    "prataap snacks", "balaji wafers", "balaji", "lehar", "uncle chipps",
    "fun flips", "hippo", "gopal", "diamondz", "pringles", "doritos",
    "cheetos", "fritos", "ruffles", "lay's", "baked", "simply agro",
    "rc cola", "pepsi co", "7 up", "real juice", "aarav", "jain",
    "vedaka", "amazon basics", "solimo", "happy belly", "whole foods",
    "mccormick", "john miller", "tasty treat", "freshness guaranteed",
    "bigbasket", "d-mart", "reliance smart", "jiomart", "flipkart grocery",
    "prestige", "hawkins", "butterfly", "pigeon", "kent", "pureit",
    "hindware", "usha", "kenstar", "black pepper", "pepper", "salt",
    "tata salt", "aashirvaad salt", "captain cook", "annapurna",
    "i-shakti", "saffola oats", "quaker", "bagrry", "post", "kellogg's",
    "kellogs", "corn flakes", "chocos", "froot loops", "special k",
    "nesquik", "milo", "bournvita", "horlicks junior", "pediasure",
    "similac", "nan", "cerelac", "nestum", "dexolac", "lactogen",
    "enfamil", "aptamil", "farex",
]
_KNOWN_BRANDS_LOWER = {b.lower() for b in KNOWN_BRANDS}

# ── Rate-limit / cooldown state (per IP) ────────────────────────────────────
_REJECTION_TRACKER: dict = {}  # ip -> {"count": int, "cooldown_until": float}
_TRACKER_LOCK = threading.Lock()

# ── OFF search headers ───────────────────────────────────────────────────────
_OFF_HEADERS = {"User-Agent": "FoodCheck-India - WebApp - Version 1.0"}
_OFF_BASE = "https://world.openfoodfacts.org/api/v2/search"


# ── Validation Helpers ───────────────────────────────────────────────────────

def validate_field(text: str, field_name: str) -> Tuple[bool, str]:
    """Validate a single search field. Returns (is_valid, error_message)."""
    if not text or not text.strip():
        return False, f"{field_name} is required."
    text = text.strip()
    if len(text) > 60:
        return False, f"{field_name} is too long (max 60 characters)."
    # Basic character whitelist
    if not _FIELD_RE.match(text):
        return False, f"{field_name} contains invalid characters."
    # Newline / multi-line injection
    if "\n" in text or "\r" in text:
        return False, f"{field_name} must not contain line breaks."
    # Injection pattern detection
    for pattern in _INJECTION_RES:
        if pattern.search(text):
            return False, "Please search only for food products and brands."
    return True, ""


def is_food_product(food: str) -> bool:
    """Quick check: is the food field an edible food category?"""
    food_lower = food.lower().strip()
    return any(
        cat in food_lower or food_lower in cat
        for cat in FOOD_CATEGORIES
    )


def is_known_brand(brand: str) -> bool:
    """Quick check: is the brand in our curated known-brands list?"""
    brand_lower = brand.lower().strip()
    return any(
        b in brand_lower or brand_lower in b
        for b in _KNOWN_BRANDS_LOWER
    )


# ── Open Food Facts Brand Verification ──────────────────────────────────────

def verify_food_off(food: str) -> bool:
    """Check if the food term exists in Open Food Facts categories/products."""
    try:
        resp = requests.get(
            _OFF_BASE,
            params={
                "search_terms": food,
                "fields": "product_name",
                "page_size": 3,
                "json": 1,
            },
            headers=_OFF_HEADERS,
            timeout=4,
        )
        if resp.status_code == 200:
            data = resp.json()
            return len(data.get("products", [])) > 0
    except Exception:
        pass
    return False


def verify_brand_off(brand: str) -> bool:
    """Check if the brand exists in Open Food Facts."""
    try:
        resp = requests.get(
            _OFF_BASE,
            params={
                "brands": brand,
                "fields": "product_name,brands",
                "page_size": 3,
                "json": 1,
            },
            headers=_OFF_HEADERS,
            timeout=4,
        )
        if resp.status_code == 200:
            data = resp.json()
            return len(data.get("products", [])) > 0
    except Exception:
        pass
    return False


# ── Gemini Binary Classification (last resort for unknown food/brand) ────────

def classify_food_brand_gemini(food: str, brand: str) -> Tuple[bool, bool]:
    """
    Use Gemini to classify whether food/brand are real food product terms.
    Returns (is_food_product: bool, is_food_brand: bool).
    User text is passed strictly as DATA inside JSON, never concatenated into instructions.
    """
    try:
        from services.gemini_client import generate_content_with_retry
        import json as _json

        system_prompt = (
            "You are a strict classifier. Given DATA containing a food_name and brand_name, "
            "you must determine ONLY: (1) is food_name an edible food, beverage, or food ingredient? "
            "(2) is brand_name a real food/beverage manufacturer or brand? "
            "Return ONLY valid JSON: {\"is_food_product\": true/false, \"is_brand\": true/false}. "
            "No explanation. No other output. If uncertain, return false."
        )

        # Pass user data as a JSON object inside the DATA wrapper — NEVER concatenate into instructions
        user_data_json = _json.dumps({"food_name": food, "brand_name": brand}, ensure_ascii=False)
        full_prompt = f"{system_prompt}\n\nDATA: {user_data_json}"

        raw = generate_content_with_retry(full_prompt, timeout=8)
        # Strip markdown code fences
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)
        result = _json.loads(clean.strip())
        return bool(result.get("is_food_product")), bool(result.get("is_brand"))
    except Exception as e:
        logger.warning(f"Gemini classify failed: {e} — defaulting to permissive")
        # On error, be permissive (avoid blocking legitimate queries due to API issues)
        return True, True


# ── Full Topic Guard ─────────────────────────────────────────────────────────

def topic_guard(food: str, brand: str) -> Tuple[bool, str]:
    """
    Multi-layer topic guard. Returns (is_valid_food_query, rejection_message).
    - Layer 1: Local curated lists (fast, no network)
    - Layer 2: Open Food Facts verification (network, with 4s timeout)
    - Layer 3: Gemini binary classification (network, only if OFF fails too)
    """
    food_ok = is_food_product(food)
    brand_ok = is_known_brand(brand)

    # Both known locally — accept immediately
    if food_ok and brand_ok:
        return True, ""

    # Try Open Food Facts for unknowns
    if not food_ok:
        food_ok = verify_food_off(food)
    if not brand_ok:
        brand_ok = verify_brand_off(brand)

    if food_ok and brand_ok:
        return True, ""

    # Last resort: Gemini classification
    gemini_food, gemini_brand = classify_food_brand_gemini(food, brand)
    if gemini_food and gemini_brand:
        return True, ""

    if not gemini_food:
        return False, (
            f"'{food}' doesn't look like a food product. "
            "Please search only for food products and brands, e.g. Biscuit > Britannia > Good Day."
        )
    return False, (
        f"'{brand}' doesn't look like a food brand. "
        "Please search only for food products and brands, e.g. Biscuit > Britannia > Good Day."
    )


# ── Rate-Limit / Cooldown Tracking ──────────────────────────────────────────

def check_cooldown(ip: str) -> Tuple[bool, str]:
    """Check if IP is in cooldown. Returns (is_blocked, message)."""
    now = time.monotonic()
    with _TRACKER_LOCK:
        state = _REJECTION_TRACKER.get(ip, {})
        cooldown_until = state.get("cooldown_until", 0)
        if now < cooldown_until:
            remaining = int(cooldown_until - now)
            return True, f"Too many invalid attempts. Please wait {remaining} seconds."
    return False, ""


def record_rejection(ip: str) -> None:
    """Record a rejected query and apply cooldown after 3 consecutive rejections."""
    now = time.monotonic()
    with _TRACKER_LOCK:
        state = _REJECTION_TRACKER.setdefault(ip, {"count": 0, "cooldown_until": 0})
        state["count"] = state.get("count", 0) + 1
        if state["count"] >= 3:
            state["cooldown_until"] = now + 60  # 1-minute cooldown
            state["count"] = 0


def record_success(ip: str) -> None:
    """Reset rejection count on a successful search."""
    with _TRACKER_LOCK:
        if ip in _REJECTION_TRACKER:
            _REJECTION_TRACKER[ip]["count"] = 0


def cleanup_tracker() -> None:
    """Prune stale IP entries (call periodically)."""
    now = time.monotonic()
    with _TRACKER_LOCK:
        stale = [
            ip for ip, s in _REJECTION_TRACKER.items()
            if now - s.get("cooldown_until", 0) > 3600
        ]
        for ip in stale:
            del _REJECTION_TRACKER[ip]
