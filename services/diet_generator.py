import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Groq model for Meta-Llama-3.1-8B-Instruct
DEFAULT_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


def calculate_bmr_and_tdee(age, gender, height_cm, weight_kg, activity_level="moderate", goal="maintain"):
    """Calculate Basal Metabolic Rate (BMR) and Total Daily Energy Expenditure (TDEE).
    Uses the clinically recognized Mifflin-St Jeor Equation.
    """
    try:
        age = float(age) if age else 25.0
        height_cm = float(height_cm) if height_cm else 170.0
        weight_kg = float(weight_kg) if weight_kg else 65.0
    except (ValueError, TypeError):
        age, height_cm, weight_kg = 25.0, 170.0, 65.0

    gender_str = str(gender).strip().lower()
    if gender_str in ["male", "m"]:
        bmr = (10.0 * weight_kg) + (6.25 * height_cm) - (5.0 * age) + 5.0
    else:
        bmr = (10.0 * weight_kg) + (6.25 * height_cm) - (5.0 * age) - 161.0

    activity_multipliers = {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "active": 1.725,
        "very_active": 1.9,
    }
    multiplier = activity_multipliers.get(str(activity_level).lower(), 1.55)
    tdee = round(bmr * multiplier)

    # Goal calorie adjustment
    goal_str = str(goal).lower().replace(" ", "_").replace("-", "_")
    if "loss" in goal_str or "deficit" in goal_str or "cut" in goal_str:
        target_calories = max(1200, round(tdee - 500))
        protein_ratio, carb_ratio, fat_ratio = 0.30, 0.40, 0.30
    elif "gain" in goal_str or "bulk" in goal_str or "muscle" in goal_str:
        target_calories = round(tdee + 350)
        protein_ratio, carb_ratio, fat_ratio = 0.30, 0.45, 0.25
    elif "keto" in goal_str:
        target_calories = round(tdee - 300)
        protein_ratio, carb_ratio, fat_ratio = 0.25, 0.05, 0.70
    elif "diabetic" in goal_str:
        target_calories = round(tdee - 200)
        protein_ratio, carb_ratio, fat_ratio = 0.25, 0.45, 0.30
    else:  # Maintenance
        target_calories = tdee
        protein_ratio, carb_ratio, fat_ratio = 0.25, 0.50, 0.25

    # Calculate target macronutrients in grams (4 kcal/g protein, 4 kcal/g carbs, 9 kcal/g fat)
    target_protein_g = round((target_calories * protein_ratio) / 4)
    target_carbs_g = round((target_calories * carb_ratio) / 4)
    target_fat_g = round((target_calories * fat_ratio) / 9)

    return {
        "bmr": round(bmr),
        "tdee": tdee,
        "target_calories": target_calories,
        "target_protein_g": target_protein_g,
        "target_carbs_g": target_carbs_g,
        "target_fat_g": target_fat_g,
    }


def generate_diet_plan(user_profile):
    """Generate a personalized daily diet plan using Groq Cloud API (Meta-Llama-3.1-8B-Instruct).
    Falls back gracefully to a calculated nutritional diet plan if API key is not yet set.
    """
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL).strip() or "llama-3.1-8b-instant"

    # Calculate baseline nutritional targets
    stats = calculate_bmr_and_tdee(
        age=user_profile.get("age"),
        gender=user_profile.get("gender"),
        height_cm=user_profile.get("height_cm"),
        weight_kg=user_profile.get("weight_kg"),
        activity_level=user_profile.get("activity_level"),
        goal=user_profile.get("goal"),
    )

    # If user has an explicit target calories override, use it
    if user_profile.get("target_calories"):
        try:
            stats["target_calories"] = int(user_profile["target_calories"])
        except (ValueError, TypeError):
            pass

    diet_pref = user_profile.get("diet_pref", "Vegetarian")
    goal = user_profile.get("goal", "Healthy Maintenance")
    health_conditions = user_profile.get("health_conditions", "None")
    user_name = user_profile.get("name", "User")

    if not groq_api_key:
        logger.warning("GROQ_API_KEY not configured. Generating scientifically computed diet template.")
        return get_fallback_diet_plan(user_name, stats, diet_pref, goal, health_conditions, is_fallback=True)

    system_prompt = (
        "You are an expert clinical dietitian and nutritionist specializing in personalized meal planning "
        "for Indian and global dietary preferences. You craft realistic, nutritionally balanced, delicious daily meal plans. "
        "Always output ONLY valid JSON without markdown fences, comments, or extra text."
    )

    user_prompt = f"""Create a highly personalized 1-day meal plan for:
- Name: {user_name}
- Age: {user_profile.get('age', 28)}, Gender: {user_profile.get('gender', 'Not specified')}
- Height: {user_profile.get('height_cm', 170)} cm, Weight: {user_profile.get('weight_kg', 68)} kg
- Activity Level: {user_profile.get('activity_level', 'Moderate')}
- Primary Goal: {goal}
- Dietary Preference: {diet_pref} (Ensure all meals strictly adhere to this!)
- Health Conditions / Notes: {health_conditions}
- Daily Targets: ~{stats['target_calories']} kcal, {stats['target_protein_g']}g Protein, {stats['target_carbs_g']}g Carbs, {stats['target_fat_g']}g Fat.

Return ONLY a single valid JSON object following this exact schema:
{{
  "plan_title": "string title",
  "daily_summary": "1-2 sentence overview of the diet strategy",
  "target_calories": {stats['target_calories']},
  "target_macros": {{
    "protein_g": {stats['target_protein_g']},
    "carbs_g": {stats['target_carbs_g']},
    "fat_g": {stats['target_fat_g']}
  }},
  "meals": [
    {{
      "meal_name": "Breakfast",
      "time": "8:30 AM",
      "items": [
        {{ "name": "Food item name with portion (e.g. 2 Moong Dal Chillas + Mint Chutney)", "calories": 280, "protein_g": 14, "carbs_g": 38, "fat_g": 7 }}
      ],
      "total_calories": 280,
      "tips": "Cooking or digestive tip"
    }},
    {{
      "meal_name": "Mid-Morning Snack",
      "time": "11:00 AM",
      "items": [
        {{ "name": "Item name with portion", "calories": 120, "protein_g": 4, "carbs_g": 18, "fat_g": 4 }}
      ],
      "total_calories": 120,
      "tips": ""
    }},
    {{
      "meal_name": "Lunch",
      "time": "1:30 PM",
      "items": [
        {{ "name": "Main dish + sides with portion", "calories": 550, "protein_g": 26, "carbs_g": 70, "fat_g": 15 }}
      ],
      "total_calories": 550,
      "tips": ""
    }},
    {{
      "meal_name": "Evening Snack",
      "time": "5:00 PM",
      "items": [
        {{ "name": "Snack item with portion", "calories": 150, "protein_g": 6, "carbs_g": 22, "fat_g": 4 }}
      ],
      "total_calories": 150,
      "tips": ""
    }},
    {{
      "meal_name": "Dinner",
      "time": "8:00 PM",
      "items": [
        {{ "name": "Dinner dish with portion", "calories": 450, "protein_g": 22, "carbs_g": 52, "fat_g": 12 }}
      ],
      "total_calories": 450,
      "tips": ""
    }}
  ],
  "hydration_plan": "Recommended water intake and timing (e.g. 3.0 Liters/day with lemon water upon waking)",
  "dietitian_notes": [
    "Tip 1 tailored to health goal",
    "Tip 2 tailored to diet preference"
  ]
}}"""

    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=25)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        plan = json.loads(content)
        plan["generated_by"] = f"Groq ({model})"
        plan["calculated_stats"] = stats
        return plan
    except requests.exceptions.HTTPError as e:
        status_code = getattr(response, "status_code", None)
        err_text = getattr(response, "text", str(e))
        logger.error(f"Groq API HTTP error {status_code}: {err_text}")
        return get_fallback_diet_plan(
            user_name, stats, diet_pref, goal, health_conditions,
            is_fallback=True, error_msg=f"Groq API Error ({status_code}): {err_text[:120]}"
        )
    except Exception as e:
        logger.error(f"Error calling Groq API: {e}", exc_info=True)
        return get_fallback_diet_plan(
            user_name, stats, diet_pref, goal, health_conditions,
            is_fallback=True, error_msg=f"API connection issue: {str(e)}"
        )


def get_fallback_diet_plan(user_name, stats, diet_pref, goal, health_conditions, is_fallback=True, error_msg=""):
    """Generates a high-quality, nutritionally balanced meal plan as a reliable backup."""
    target_cals = stats["target_calories"]
    diet = str(diet_pref).lower()
    is_non_veg = "non" in diet or "chicken" in diet or "fish" in diet or "meat" in diet

    if is_non_veg:
        breakfast_item = "3 Boiled Eggs (2 whites, 1 whole) + 2 Whole Wheat Toast + 1 cup Green Tea"
        b_prot, b_carb, b_fat = 20, 28, 8
        lunch_item = "150g Grilled / Steamed Chicken Breast + 1 cup Brown Rice + Stir-fried Veggies + Cucumber Salad"
        l_prot, l_carb, l_fat = 38, 48, 10
        dinner_item = "150g Baked Fish / Chicken Curry (low oil) + 2 Phulkas + 1 bowl Mixed Salad"
        d_prot, d_carb, d_fat = 32, 35, 11
    elif "vegan" in diet:
        breakfast_item = "Tofu Scramble (150g) with spinach, bell peppers + 2 Multigrain Toasts"
        b_prot, b_carb, b_fat = 18, 30, 9
        lunch_item = "1.5 bowls Rajma / Chickpea Curry + 1 cup Brown Rice + Fresh Kachumber Salad"
        l_prot, l_carb, l_fat = 22, 65, 8
        dinner_item = "1 bowl Sprouted Moong Stir-fry + 2 Bajra/Jowar Rotis + 1 bowl Steamed Veggies"
        d_prot, d_carb, d_fat = 18, 52, 6
    else:  # Vegetarian (default)
        breakfast_item = "2 Stuffed Paneer / Moong Dal Chillas (approx 150g) + Mint Green Chutney"
        b_prot, b_carb, b_fat = 18, 32, 10
        lunch_item = "1 bowl Yellow Dal Tadka + 100g Low-fat Paneer Bhurji + 2 Phulkas + 1 cup Curd"
        l_prot, l_carb, l_fat = 26, 56, 14
        dinner_item = "1 bowl Mixed Dal Palak + 2 Multigrain Phulkas + Cucumber-Tomato Salad"
        d_prot, d_carb, d_fat = 18, 48, 9

    b_cal = round((b_prot * 4) + (b_carb * 4) + (b_fat * 9))
    l_cal = round((l_prot * 4) + (l_carb * 4) + (l_fat * 9))
    d_cal = round((d_prot * 4) + (d_carb * 4) + (d_fat * 9))

    snack1_cal = round(target_cals * 0.08)
    snack2_cal = round(target_cals * 0.08)

    return {
        "plan_title": f"Personalized {goal.title()} Meal Plan for {user_name}",
        "daily_summary": f"Scientifically calibrated for ~{target_cals} kcal tailored for {diet_pref.title()} preferences and {goal.title()} goal.",
        "target_calories": target_cals,
        "target_macros": {
            "protein_g": stats["target_protein_g"],
            "carbs_g": stats["target_carbs_g"],
            "fat_g": stats["target_fat_g"],
        },
        "meals": [
            {
                "meal_name": "Breakfast",
                "time": "8:30 AM",
                "items": [
                    {"name": breakfast_item, "calories": b_cal, "protein_g": b_prot, "carbs_g": b_carb, "fat_g": b_fat}
                ],
                "total_calories": b_cal,
                "tips": "Eat within 1 hour of waking to boost metabolism."
            },
            {
                "meal_name": "Mid-Morning Snack",
                "time": "11:00 AM",
                "items": [
                    {"name": "1 Apple / Papaya bowl (120g) + 6 Almonds & 2 Walnuts", "calories": snack1_cal, "protein_g": 3, "carbs_g": 18, "fat_g": 6}
                ],
                "total_calories": snack1_cal,
                "tips": "Healthy fats and fiber keep hunger hormones in check."
            },
            {
                "meal_name": "Lunch",
                "time": "1:30 PM",
                "items": [
                    {"name": lunch_item, "calories": l_cal, "protein_g": l_prot, "carbs_g": l_carb, "fat_g": l_fat}
                ],
                "total_calories": l_cal,
                "tips": "Start lunch with your salad to promote satiety and blunt glucose spikes."
            },
            {
                "meal_name": "Evening Snack",
                "time": "5:00 PM",
                "items": [
                    {"name": "1 cup Roasted Makhana (Foxnuts) + Green Tea (no sugar)", "calories": snack2_cal, "protein_g": 4, "carbs_g": 20, "fat_g": 2}
                ],
                "total_calories": snack2_cal,
                "tips": "Prevents evening binge eating before dinner."
            },
            {
                "meal_name": "Dinner",
                "time": "8:00 PM",
                "items": [
                    {"name": dinner_item, "calories": d_cal, "protein_g": d_prot, "carbs_g": d_carb, "fat_g": d_fat}
                ],
                "total_calories": d_cal,
                "tips": "Keep dinner light and finish at least 2 hours before bedtime."
            }
        ],
        "hydration_plan": "Drink 2.5 - 3.5 Liters of water throughout the day. Avoid large glasses during meals.",
        "dietitian_notes": [
            f"Tailored to your daily goal of {target_cals} kcal ({stats['target_protein_g']}g Protein target).",
            "Focus on whole food ingredients and minimize ultra-processed foods.",
            "Use minimal cold-pressed oils (mustard, olive, or groundnut oil) for cooking."
        ],
        "is_fallback": is_fallback,
        "note": "Connect GROQ_API_KEY to unlock dynamic Meta-Llama-3.1 generation directly from Groq Cloud." if not error_msg else error_msg,
        "calculated_stats": stats,
    }
