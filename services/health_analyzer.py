import logging
from typing import Optional, Dict, Any
from services.ai_provider import analyze_food_health

logger = logging.getLogger(__name__)


def analyze(ingredients_text: str, nutrition_facts: Optional[Dict[str, Any]], product_name: str = "") -> dict:
    """Analyze packaged food product using multi-provider AI architecture.
    
    Fast generation & extended limit features:
    - Automatically uses Groq LPU (Llama-3.3-70B) for ultra-fast (~300ms) scoring if GROQ_API_KEY is present.
    - Cascades to Google Gemini Multi-Key Pool (GEMINI_API_KEYS / GEMINI_API_KEY_1..5) with auto 429 key rotation.
    - Fallback to OpenRouter / OpenAI if quotas are exhausted.
    
    Returns:
        dict with keys: score (0-100), verdict (safe/moderate/unsafe),
        flags (list), reasoning (str), alternatives (list)
    """
    return analyze_food_health(
        ingredients_text=ingredients_text,
        nutrition_facts=nutrition_facts,
        product_name=product_name,
    )
