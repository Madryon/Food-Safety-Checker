import google.generativeai as genai
import json
import os


def analyze(ingredients_text: str, nutrition_facts: dict | None, product_name: str = "") -> dict:
    """Send ingredients and nutrition to Gemini for health/safety scoring.
    
    Returns:
        dict with keys: score (0-100), verdict (safe/moderate/unsafe),
        flags (list), reasoning (str)
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return _fallback_response("Missing GEMINI_API_KEY. Please set your key in .env.")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3.6-flash")

    nutrition_str = json.dumps(nutrition_facts, indent=2) if nutrition_facts else "Not available"

    prompt = f"""You are a food safety and health analyst specializing in the Indian market (FSSAI regulations).

Analyze the following packaged food product and provide a health/safety assessment.

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
5. Protein content misleading vs. product's health claims (e.g., product marketed as "protein bar" but has < 10g protein per 100g) — flag as MISLEADING CLAIMS
6. Allergens present (nuts, dairy, gluten, soy) — flag as ALLERGEN with specifics
7. High sodium (> 500mg per 100g) — flag as HIGH SODIUM
8. Ultra-processed indicators (long ingredient list with many chemical additives) — flag as ULTRA-PROCESSED

=== VERDICT MAPPING ===
- Score 70-100: verdict = "safe"
- Score 40-69: verdict = "moderate" 
- Score 0-39: verdict = "unsafe"

=== ALTERNATIVES SUGGESTION ===
Suggest 2 to 3 healthier or cleaner alternative products or food options available in the Indian market for this specific product or category (e.g., if instant noodles with palm oil/maida, suggest millet noodles like Slurrp Farm or whole wheat noodles; if sugary biscuits, suggest whole oat or nut-based clean snacks; if ultra-processed chips, suggest roasted makhana or vacuum-cooked vegetable crisps).

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

Do NOT wrap in markdown code blocks. Return raw JSON only.
"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Clean up potential markdown wrapping
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        result = json.loads(text)
        
        # Validate required keys
        required = {"score", "verdict", "flags", "reasoning"}
        if not required.issubset(result.keys()):
            missing = required - set(result.keys())
            return _fallback_response(f"AI response missing keys: {missing}")
        
        # Ensure score is int and verdict is valid
        result["score"] = int(result["score"])
        if result["verdict"] not in ("safe", "moderate", "unsafe"):
            if result["score"] >= 70:
                result["verdict"] = "safe"
            elif result["score"] >= 40:
                result["verdict"] = "moderate"
            else:
                result["verdict"] = "unsafe"
        
        # Ensure alternatives is a list
        if "alternatives" not in result or not isinstance(result["alternatives"], list):
            result["alternatives"] = []

        return result
    
    except json.JSONDecodeError:
        return _fallback_response("Could not parse AI health analysis response.")
    except Exception as e:
        return _fallback_response(f"Health analysis failed: {str(e)}")


def _fallback_response(error_msg: str) -> dict:
    """Return a safe fallback when analysis fails."""
    return {
        "score": None,
        "verdict": "unknown",
        "flags": [],
        "alternatives": [],
        "reasoning": error_msg,
    }
