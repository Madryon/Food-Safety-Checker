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
_FIELD_RE = re.compile(r"^[a-zA-Z0-9 \',.\-&()/+%:;\"!*éèêëàâûùüîïôœç]+$")

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

# ── Out-of-Context & Non-Food Patterns ──────────────────────────────────────
_OUT_OF_CONTEXT_PATTERNS = [
    # Programming & Software
    r"\b(python|javascript|typescript|c\+\+|c#|golang|rust|ruby|php|swift|kotlin|scala|html|css|sql|nosql)\b",
    r"\b(code|coding|program|programming|script|scripting|algorithm|function|variable|class|object|method|loop)\b",
    r"\b(database|table|query|select|insert|update|delete|drop|schema|index|join|foreign\s*key|primary\s*key)\b",
    r"\b(server|client|api|endpoint|backend|frontend|framework|library|repository|repo|github|git|commit|pull\s*request|merge)\b",
    r"\b(software|hardware|firmware|operating\s*system|windows|linux|macos|ubuntu|android\s*os|ios)\b",
    r"\b(laptop|computer|desktop|pc|monitor|screen|keyboard|mouse|processor|cpu|gpu|ram|motherboard|hard\s*drive|ssd)\b",
    r"\b(phone|smartphone|iphone|ipad|tablet|charger|battery|cable|usb|bluetooth|wifi|router|modem)\b",
    r"\b(terminal|powershell|bash|cmd|shell|command\s*line|docker|kubernetes|container|aws|azure|gcp|cloud)\b",
    r"\b(ai|artificial\s*intelligence|machine\s*learning|deep\s*learning|neural\s*network|chatgpt|openai|gemini|llm|bot|chatbot|prompt)\b",
    r"\b(compiler|interpreter|debugger|stack\s*trace|nullpointer|syntax\s*error|exception|runtime)\b",

    # Conversational questions & homework prompts (NOT a chatbot)
    r"\b(how\s+to|what\s+is|who\s+is|where\s+is|why\s+is|when\s+is|when\s+was|which\s+is)\b",
    r"\b(can\s+you|could\s+you|tell\s+me|explain|describe|write\s+me|summarize|translate|solve|calculate)\b",
    r"\b(meaning\s+of|definition\s+of|homework|essay|assignment|math\s+problem|formula|equation)\b",
    r"\b(hello|hi\b|hey\b|how\s+are\s+you|good\s+morning|good\s+night|bye|testing|test\s*123)\b",

    # Non-food inanimate objects & vehicles
    r"\b(car|cars|automobile|vehicle|truck|bus|motorcycle|bike|bicycle|scooter|engine|tire|tyre|wheel|petrol|diesel)\b",
    r"\b(clothes|clothing|shirt|t-shirt|pant|pants|jeans|trousers|dress|suit|jacket|coat|shoes|sneakers|socks|hat|cap)\b",
    r"\b(furniture|chair|chairs|desk|desks|sofa|bed|mattress|cupboard|wardrobe|bookshelf)\b",
    r"\b(pen|pencil|paper|stationery|book|books|notebook|eraser|ruler|scissors|stapler)\b",
    r"\b(house|building|apartment|flat|room|wall|roof|cement|brick|paint|wood|metal|iron|steel|plastic|glass)\b",
    r"\b(money|cash|dollar|dollars|rupee|rupees|currency|crypto|cryptocurrency|bitcoin|ethereum|stock|shares)\b",
]
_OUT_OF_CONTEXT_RES = [re.compile(p, re.IGNORECASE) for p in _OUT_OF_CONTEXT_PATTERNS]


def is_out_of_context(text: str) -> Tuple[bool, str]:
    """
    Check if a query contains programming, technical, conversational, or non-food keywords.
    Returns (is_out_of_context: bool, warning_message: str).
    """
    if not text or not text.strip():
        return False, ""
    t_low = text.lower().strip()

    # Legitimate food exceptions that contain words like 'table' or 'apple'
    food_exceptions = ["table salt", "table butter", "table wine", "apple pie", "apple juice", "blackberry", "shellfish", "coffee table book"]
    t_check = t_low
    for ex in food_exceptions:
        t_check = t_check.replace(ex, " ")
    t_check = t_check.strip()
    if not t_check and any(ex in t_low for ex in food_exceptions):
        return False, ""

    for pattern in _OUT_OF_CONTEXT_RES:
        m = pattern.search(t_check)
        if m:
            return True, (
                f"Out of context: '{text}' does not appear to be an edible food or beverage. "
                "Please check your input — there may be an error or typo. "
                "FoodCheck only analyzes food products, snacks, and beverages "
                "(e.g. 'Mother Dairy Paneer', 'Maggi Noodles', 'Diet Coke', 'Good Day Cookies')."
            )

    return False, ""

# ── Curated food categories (lowercase) ─────────────────────────────────────
FOOD_CATEGORIES = [
    # Bakery & Biscuits
    "biscuit", "cookies", "cookie", "cracker", "rusk", "bread stick",
    "bread", "multigrain bread", "brown bread", "white bread",
    "cake", "pastry", "muffin", "cupcake", "brownie", "wafer",
    # Noodles, Pasta & Instant Food
    "noodles", "noodle", "instant noodles", "maggi", "pasta", "macaroni",
    "vermicelli", "spaghetti", "yippee", "top ramen",
    "ready to eat", "instant food", "frozen food", "canned food",
    # Snacks & Chips
    "chips", "potato chips", "multigrain chips", "baked chips", "puffed snack",
    "popcorn", "namkeen", "kurkure", "doritos", "lay's", "lays", "pringles",
    "papad", "wafer", "nachos", "tortilla chips", "corn snack", "trail mix",
    # Chocolate & Confectionery
    "chocolate", "chocolate bar", "dark chocolate", "milk chocolate",
    "dairy milk", "munch", "kitkat", "kit kat", "5 star", "perk", "eclairs",
    "candy", "toffee", "lollipop", "gummy", "jelly candy", "hard candy",
    "oreo", "bourbon", "marie", "good day", "hide and seek",
    # Dairy & Alternatives
    "milk", "low fat milk", "skimmed milk", "toned milk", "full cream milk",
    "plant based milk", "almond milk", "soy milk", "oat milk", "coconut milk",
    "curd", "yogurt", "greek yogurt", "low fat yogurt", "probiotic yogurt",
    "paneer", "cheese", "processed cheese", "cheese slice", "cream cheese",
    "butter", "ghee", "cream", "kulfi", "ice cream", "frozen dessert",
    "lassi", "buttermilk", "chhach", "shrikhand", "rasgulla",
    # Beverages
    "juice", "fruit juice", "fruit drink", "nectar", "fruit punch",
    "soft drink", "cola", "carbonated drink", "aerated drink", "sparkling water",
    "energy drink", "sports drink", "electrolyte drink", "glucose drink",
    "coconut water", "mineral water", "packaged water", "flavoured water",
    "smoothie", "milkshake", "health drink", "malt drink",
    "tea", "green tea", "herbal tea", "black tea", "iced tea", "chai",
    "coffee", "instant coffee", "cold coffee",
    "bournvita", "horlicks", "complan", "boost", "milo", "ovaltine",
    "oral rehydration",
    # Cereals & Breakfast
    "cereal", "breakfast cereal", "corn flakes", "wheat flakes", "muesli",
    "granola", "oats", "rolled oats", "instant oats", "oatmeal",
    "chocos", "froot loops", "special k", "bagrry",
    # Flours, Grains & Staples
    "rice", "basmati rice", "brown rice", "white rice",
    "wheat flour", "atta", "multigrain atta", "whole wheat flour",
    "maida", "suji", "semolina", "besan", "chickpea flour", "ragi flour",
    "dal", "lentil", "moong dal", "chana dal", "rajma", "kidney beans",
    # Oils & Fats
    "cooking oil", "sunflower oil", "olive oil", "mustard oil",
    "coconut oil", "palm oil", "soybean oil", "refined oil",
    "rice bran oil", "groundnut oil", "canola oil", "vanaspati", "margarine",
    # Condiments & Spreads
    "ketchup", "tomato sauce", "mayonnaise", "mustard", "vinegar",
    "soy sauce", "hot sauce", "chilli sauce", "tamarind sauce",
    "peanut butter", "almond butter", "nutella", "hazelnut spread",
    "jam", "marmalade", "jelly", "honey", "syrup", "maple syrup",
    "pickle", "chutney", "relish", "dip", "hummus", "tahini",
    "curry paste", "biryani mix", "pulao mix", "khichdi mix",
    "dosa mix", "idli mix", "upma mix",
    # Spices & Seasoning
    "salt", "sugar", "spices", "masala", "chaat masala", "garam masala",
    "sambhar masala", "everest masala", "mdh masala",
    # Protein & Health Supplements
    "protein bar", "energy bar", "granola bar", "nutrition bar", "snack bar",
    "protein powder", "whey protein", "whey", "casein protein",
    "plant protein", "pea protein", "soy protein",
    "supplement", "vitamin", "multivitamin", "probiotic", "prebiotic",
    "meal replacement", "protein shake", "glucose", "dextrose",
    # Diet & Special Diet Labels (FIX 1)
    "sugar free", "sugar-free", "no added sugar", "diabetic friendly",
    "diet", "diet food", "diet biscuit", "diet chips", "diet soda",
    "low fat", "low-fat", "fat free", "skimmed", "low calorie", "light",
    "low sodium", "low-sodium", "low salt",
    "organic", "certified organic", "natural", "all natural", "clean label",
    "whole grain", "whole wheat", "multigrain", "high fibre", "high fiber",
    "gluten free", "gluten-free", "wheat free", "celiac friendly",
    "vegan", "plant based", "plant-based", "vegan chocolate", "vegan snack",
    "vegetarian", "jain", "sattvic", "halal", "kosher",
    "keto", "ketogenic", "keto snack", "keto bar", "low carb", "low-carb",
    "paleo", "paleo bar", "paleo snack",
    "dairy free", "dairy-free", "lactose free", "lactose-free",
    "nut free", "allergen free", "non gmo", "non-gmo",
    "fortified", "enriched", "added vitamins", "high protein",
    # Nuts & Seeds
    "nuts", "almonds", "cashews", "peanuts", "pistachios", "walnuts",
    "macadamia", "brazil nuts", "pecans", "hazelnuts",
    "seeds", "flaxseeds", "chia seeds", "sunflower seeds", "pumpkin seeds",
    "hemp seeds", "sesame seeds", "dried fruit", "raisins", "dates",
    # Indian Sweets
    "sweets", "mithai", "indian sweets", "gulab jamun", "halwa",
    "ladoo", "barfi", "peda", "jalebi", "kheer",
    "dessert", "pudding", "custard", "mousse",
    # Soups
    "soup", "instant soup", "knorr soup", "cream soup", "vegetable soup",
    # Baby & Infant Food
    "baby food", "infant formula", "cerelac", "nestum", "farex", "toddler food",
    # Miscellaneous packaged
    "tofu", "sprouts", "condiment", "sauce", "preserved food",
]

# ── Curated brand list (lowercase for matching) ──────────────────────────────
KNOWN_BRANDS = [
    # Indian FMCG & Food majors
    "britannia", "parle", "sunfeast", "nestle", "itc", "haldiram's", "haldirams",
    "amul", "cadbury", "mondelez", "pepsico", "hindustan unilever", "hul",
    # Snacks
    "lay's", "lays", "kurkure", "lehar", "bingo", "uncle chipps",
    "cornitos", "doritos", "pringles", "cheetos", "fritos", "ruffles",
    "too yumm", "jabsons", "balaji", "balaji wafers", "gopal", "bikano", "bikaji",
    "haldiram", "garden", "bikaner", "prataap snacks", "anmol",
    # Chocolate & Confectionery
    "maggi", "munch", "kitkat", "kit kat", "dairy milk", "5 star", "perk",
    "eclairs", "bounty", "snickers", "mars", "milkybar", "toblerone",
    "ferrero", "lindt", "hershey's", "hersheys",
    # Noodles & Pasta
    "yippee", "top ramen", "ching's", "chings", "knorr", "maggi",
    # Health Drinks
    "bournvita", "horlicks", "complan", "boost", "milo", "ovaltine", "pediasure",
    "ensure", "glucon-d", "glucon d", "glucose d", "hlth",
    # Beverages
    "frooti", "real", "paperboat", "appy fizz", "maaza", "minute maid",
    "tropicana", "coke", "coca cola", "pepsi", "7up", "sprite", "fanta",
    "mirinda", "mountain dew", "thums up", "limca", "slice", "nimbooz",
    "sting", "red bull", "monster", "rockstar", "cloud 9", "tzinga",
    "rc cola", "b natural",
    # Tea & Coffee
    "lipton", "brooke bond", "tata tea", "taj mahal", "red label", "sunrise",
    "bru", "nescafe", "nescafe gold", "tetley", "wagh bakri", "tapal",
    "typhoo", "dilmah", "twinings", "girnar",
    # Staples & Flours
    "aashirvaad", "patanjali", "dabur", "marico", "fortune",
    "saffola", "nature fresh", "pillsbury", "mtr", "gits",
    "kissan", "heinz", "i-shakti", "annapurna", "captain cook",
    "tata salt", "catch", "everest", "mdh", "badshah", "swad", "pushp",
    "india gate", "kohinoor", "daawat", "basmati house", "lal qilla",
    "shaktibhog", "rolly poly",
    # Dairy
    "mother dairy", "kwality walls", "kwality", "creambell", "havmor",
    "baskin robbins", "naturals", "vadilal", "dinshaw's", "dinshaws",
    "amul", "id fresh", "epigamia",
    # Cereals & Oats
    "quaker", "kellogg's", "kellogs", "bagrry's", "bagrrys",
    "saffola oats", "post", "nesquik", "chocos",
    # Oils & Fats
    "fortune", "saffola", "sundrop", "dhara", "parachute", "gold winner",
    "gemini", "ruchi", "emami", "kaleesuwari",
    # Health & Wellness Foods (FIX 2 - new brands)
    "yoga bar", "yogabar", "ritebite", "rite bite", "max protein",
    "my fitness", "myfitness", "oziva", "kapiva", "himalaya",
    "slurrp farm", "early foods", "timios", "bagrry's",
    "nourish organics", "true elements", "organic india", "24 mantra",
    "fabindia", "wingreens", "wingreens farms",
    "kind", "kind bar", "larabar", "rxbar", "clif bar", "quest bar",
    "muscleblaze", "muscle blaze", "myprotein", "my protein",
    "healthkart", "gnc", "optimum nutrition", "isopure",
    "dymatize", "bsn", "met rx",
    # Convenience & Modern Brands
    "mccain", "acts ii", "act ii", "fun flips", "hippo", "diamondz",
    "simply agro", "veeba", "borges", "del monte", "delmonte",
    "tasty treat", "happy belly", "vedaka",
    # Patanjali & Ayurvedic
    "patanjali", "baidyanath", "zandu", "hamdard",
    "himalayan", "dabur", "emami",
    # Baby Food
    "nestle cerelac", "cerelac", "nestum", "farex", "dexolac",
    "lactogen", "nan", "similac", "enfamil", "aptamil",
    # Sauces, Pickles & Condiments
    "kissan", "heinz", "maggi ketchup", "druk", "maiyas",
    "mother's recipe", "priya", "aachi", "eastern", "lijjat",
    # Indian Regional
    "bikaner raj", "raj", "bikanervala", "haldiram bhujiawala",
    "bombay halwa", "ganesh",
    # International brands sold in India
    "pran", "mccormick", "john miller", "britannia tiger",
    "orion", "pepero", "pocky", "meiji", "morinaga",
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
    if len(text) > 120:
        return False, f"{field_name} is too long (max 120 characters)."
    # Basic character whitelist
    if not _FIELD_RE.match(text):
        return False, f"{field_name} contains invalid characters."
    # Newline / multi-line injection
    if "\n" in text or "\r" in text:
        return False, f"{field_name} must not contain line breaks."
    # Injection pattern detection
    for pattern in _INJECTION_RES:
        if pattern.search(text):
            return False, (
                f"Out of context: '{text}' does not appear to be an edible food or beverage. "
                "Please check your input — there may be an error or typo. "
                "FoodCheck only analyzes food products, snacks, and beverages."
            )
    # Out of context non-food detection
    is_ooc, ooc_msg = is_out_of_context(text)
    if is_ooc:
        return False, ooc_msg
    return True, ""


def is_food_product(food: str) -> bool:
    """Check if the food string contains a recognized edible food category or staple."""
    if not food or not food.strip():
        return False
    food_lower = food.lower().strip()
    words = set(re.findall(r'[a-z0-9]+', food_lower))

    # Check curated food categories
    for cat in FOOD_CATEGORIES:
        cat_lower = cat.lower()
        if " " in cat_lower:
            if cat_lower in food_lower:
                return True
        elif cat_lower in words:
            return True

    # Check known brands
    for b in _KNOWN_BRANDS_LOWER:
        if " " in b:
            if b in food_lower:
                return True
        elif b in words:
            return True

    common_food_kw = {
        "biscuit", "biscuits", "cookie", "cookies", "rusk", "wafer", "wafers", "noodle", "noodles", "pasta", "ramen", "maggi",
        "chip", "chips", "crisp", "crisps", "snack", "snacks", "namkeen", "bhujia", "sev", "papad", "popcorn", "makhana",
        "chocolate", "chocolates", "candy", "sweet", "sweets", "bar", "toffee", "gum", "cake", "bread",
        "bun", "butter", "cheese", "paneer", "panner", "paner", "panneer", "milk", "curd", "yogurt", "cream",
        "ghee", "oil", "juice", "drink", "drinks", "beverage", "beverages", "soda", "cola", "coke", "shake",
        "tea", "coffee", "cereal", "oats", "cornflakes", "muesli", "granola",
        "sauce", "ketchup", "jam", "spread", "mayonnaise", "dip", "soup",
        "dal", "rice", "flour", "atta", "maida", "suji", "besan", "wheat",
        "salt", "sugar", "spice", "spices", "masala", "powder", "pickle", "protein", "whey",
        "egg", "eggs", "meat", "chicken", "fish", "tofu", "soya", "soy", "almond", "cashew", "peanut", "walnut"
    }
    return bool(words.intersection(common_food_kw))


def is_generic_brand_placeholder(brand: str) -> bool:
    """Returns True if the brand is empty or user selected a generic/unspecified placeholder."""
    b = (brand or "").lower().strip()
    return (not b or b in (
        "not sure", "any", "unknown", "generic", "various",
        "any brand", "not specified", "none", "n/a", "na", "all",
        "generic / assorted", "commercial food", "packaged food"
    ))


def is_known_brand(brand: str) -> bool:
    """Returns True ONLY if the brand is a recognized commercial food/beverage brand."""
    b = (brand or "").lower().strip()
    if not b or is_generic_brand_placeholder(b):
        return False
    words = set(re.findall(r'[a-z0-9]+', b))
    for kb in _KNOWN_BRANDS_LOWER:
        if " " in kb:
            if kb in b:
                return True
        elif kb in words:
            return True
    return False


# ── Open Food Facts Brand Verification ──────────────────────────────────────

def verify_food_off(food: str) -> bool:
    """Check if the food term exists in Open Food Facts categories/products."""
    if not food or not food.strip():
        return False
    try:
        resp = requests.get(
            _OFF_BASE,
            params={
                "search_terms": food,
                "fields": "product_name,categories_tags",
                "page_size": 3,
                "json": 1,
            },
            headers=_OFF_HEADERS,
            timeout=4,
        )
        if resp.status_code == 200:
            data = resp.json()
            prods = data.get("products", [])
            if prods and any(p.get("product_name") for p in prods):
                return True
    except Exception:
        pass
    return False


def verify_brand_off(brand: str) -> bool:
    """Check if the brand exists in Open Food Facts."""
    if not brand or not brand.strip() or is_generic_brand_placeholder(brand):
        return False
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
            "You are a strict food classifier. Given DATA containing a food_name and brand_name, "
            "you must determine ONLY: (1) is food_name an edible food, beverage, or food ingredient? "
            "(2) is brand_name a real food/beverage manufacturer or brand? "
            "If the text is programming code, technical jargon, electronics, questions, or non-food items, return false. "
            "Return ONLY valid JSON: {\"is_food_product\": true/false, \"is_brand\": true/false}. "
            "No explanation. No other output."
        )

        user_data_json = _json.dumps({"food_name": food, "brand_name": brand}, ensure_ascii=False)
        full_prompt = f"{system_prompt}\n\nDATA: {user_data_json}"

        raw = generate_content_with_retry(full_prompt, timeout=3)
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)
        result = _json.loads(clean.strip())
        return bool(result.get("is_food_product")), bool(result.get("is_brand"))
    except Exception as e:
        logger.warning(f"Gemini classify failed: {e} — defaulting to strict")
        return False, False


# ── Full Topic Guard ─────────────────────────────────────────────────────────

def topic_guard(food: str, brand: str) -> Tuple[bool, str]:
    """
    Multi-layer topic guard:
    1. Out-of-context check (coding, software, general questions, hardware, furniture, etc.)
    2. Curated food intelligence catalog match (instant 0ms)
    3. Food categories & staples verification
    4. Open Food Facts real product verification
    Returns (is_valid: bool, rejection_message: str).
    """
    clean_f = (food or "").strip()
    clean_b = (brand or "").strip()

    warning_msg = (
        f"Out of context: '{clean_f}' does not appear to be an edible food or beverage. "
        "Please check your input — there may be an error or typo. "
        "FoodCheck only analyzes food products, snacks, and beverages "
        "(e.g. 'Mother Dairy Paneer', 'Maggi Noodles', 'Diet Coke', 'Good Day Cookies')."
    )

    # 1. Out-of-context check
    is_ooc, ooc_msg = is_out_of_context(f"{clean_f} {clean_b}".strip())
    if is_ooc:
        return False, ooc_msg

    # 2. Curated food intelligence catalog match (e.g. 'Mother Dairy Paneer', 'Diet Coke', 'Maggi')
    from services.food_knowledge import find_in_knowledge_base
    if find_in_knowledge_base(clean_f, clean_b, clean_f):
        return True, ""

    food_ok = is_food_product(clean_f)
    brand_ok = is_known_brand(clean_b)
    brand_is_gen = is_generic_brand_placeholder(clean_b)

    # 3. Direct recognition:
    if food_ok:
        if brand_is_gen or brand_ok:
            return True, ""
        # If user gave an unlisted brand, check Open Food Facts for the brand
        if verify_brand_off(clean_b):
            return True, ""
        # Even if brand is unfamiliar, if food category is definitely recognized, accept
        return True, ""

    if brand_ok and not brand_is_gen:
        # Check if the food item exists under that brand or in Open Food Facts
        if verify_food_off(f"{clean_b} {clean_f}"):
            return True, ""

    # 4. Open Food Facts verification for unknown items
    if verify_food_off(clean_f):
        return True, ""

    # 5. Gemini binary classification fallback (strict)
    gemini_food, gemini_brand = classify_food_brand_gemini(clean_f, clean_b)
    if gemini_food:
        return True, ""

    return False, warning_msg


# ── Rate-Limit / Cooldown Tracking ──────────────────────────────────────────

def check_cooldown(ip: str) -> Tuple[bool, str]:
    """Check if IP is in cooldown. Returns (is_blocked, message)."""
    try:
        from flask import current_app
        if current_app and current_app.config.get("TESTING"):
            return False, ""
    except Exception:
        pass
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
