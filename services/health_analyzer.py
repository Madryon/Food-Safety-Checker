import logging
from typing import Optional, Dict, Any
from services.ai_provider import analyze_food_health

logger = logging.getLogger(__name__)


def analyze(ingredients_text: str, nutrition_facts: Optional[Dict[str, Any]], product_name: str = "") -> dict:
    """Analyze packaged food product using multi-provider AI architecture
    with FSSAI Safety Intelligence Engine fallback.
    
    Fast generation & extended limit features:
    - Automatically uses Groq LPU (Llama-3.3-70B) for ultra-fast (~300ms) scoring if GROQ_API_KEY is present.
    - Cascades to Google Gemini Multi-Key Pool (GEMINI_API_KEYS / GEMINI_API_KEY_1..5) with auto 429 key rotation.
    - Medical-grade FSSAI Safety Intelligence Engine fallback if cloud AI is unavailable.
    
    Returns:
        dict with keys: score (0-100), verdict (safe/moderate/unsafe),
        flags (list), reasoning (str), alternatives (list)
    """
    res = analyze_food_health(
        ingredients_text=ingredients_text,
        nutrition_facts=nutrition_facts,
        product_name=product_name,
    )
    if res and isinstance(res.get("flags"), list):
        res["flags"] = [
            f for f in res["flags"]
            if isinstance(f, dict) and f.get("type") not in ("AI_ANALYSIS_BUSY", "AI_ANALYSIS_OFFLINE")
        ]
    return res
