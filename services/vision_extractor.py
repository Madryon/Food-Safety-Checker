import google.generativeai as genai
import json
import os
from PIL import Image


def extract(image_path: str) -> dict:
    """Send image to Gemini Vision and extract product information.

    Returns:
        dict with keys: brand, product_name, ingredients_text, nutrition_facts
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {"error": "Missing GEMINI_API_KEY. Please add your key to the .env file."}

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3.6-flash")

    img = Image.open(image_path)

    prompt = """Look at this image of a packaged food product sold in India.

Your job is to extract as much information as possible.

1. brand - The brand or company name visible on the packaging. If you can identify the product but can't read the brand clearly, use your knowledge to infer it.
2. product_name - The specific product name. If partially visible, give your best guess.
3. ingredients_text - The ingredients list as printed on the package if visible. If the ingredients list is NOT visible in the image (e.g., only the front of the pack is shown), then USE YOUR KNOWLEDGE to provide the typical/known ingredients for this specific product. Prefix with "[AI-sourced] " to indicate it's from your knowledge, not the image.
4. nutrition_facts - Nutritional values per 100g as a dictionary with keys: energy_kcal, protein_g, total_fat_g, saturated_fat_g, trans_fat_g, carbohydrates_g, total_sugar_g, sodium_mg, fiber_g. If nutrition info is NOT visible in the image, USE YOUR KNOWLEDGE of this product to fill in approximate/typical values. Use null ONLY if you genuinely don't know.
5. data_source - Set to "image" if you read the data from the image, or "ai_knowledge" if you filled in from your knowledge, or "partial" if some from image and some from knowledge.

IMPORTANT RULES:
- ALWAYS try to identify the product. Even if you can only see the front of the package, identify the brand and product name.
- Use your extensive knowledge of Indian food products (Maggi, Parle-G, Britannia, Amul, Haldiram's, Lays, Kurkure, etc.) to provide ingredients and nutrition data when not visible in the image.
- Do NOT leave ingredients or nutrition empty. If not visible, provide from your knowledge.
- Only return an error if the image contains NO food product at all.

Return ONLY valid JSON with these exact keys: brand, product_name, ingredients_text, nutrition_facts, data_source
Do NOT wrap in markdown code blocks. Return raw JSON only.

Only if the image does NOT contain any food product at all, return:
{"error": "This does not appear to be a food product image. Please upload a photo of a packaged food item."}
"""

    try:
        response = model.generate_content([prompt, img])
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
        return result
    except json.JSONDecodeError:
        return {"error": "Could not parse the AI response. Please try again with a clearer photo."}
    except Exception as e:
        return {"error": f"Vision analysis failed: {str(e)}"}
