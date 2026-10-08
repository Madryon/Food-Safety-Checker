import os
import uuid
import shutil
import json
from datetime import datetime

from flask import Flask, request, render_template, jsonify, session
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from services.vision_extractor import extract
from services.product_lookup import search
from services.health_analyzer import analyze
from services.news_fetcher import get_news
from services.database import (
    init_db,
    save_user_profile,
    get_user_profile,
    save_diet_plan,
    get_diet_plan,
    get_user_diet_plans,
    log_meal,
    get_daily_calorie_summary,
    delete_meal_log,
    register_account,
    authenticate_account,
    get_account,
    save_feedback_db,
    get_user_feedbacks,
    get_developer_feedbacks,
    get_all_feedbacks,
    update_feedback_db
)
from services.diet_generator import generate_diet_plan, calculate_bmr_and_tdee, suggest_quick_meal


load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB
app.secret_key = os.getenv("SECRET_KEY", "foodcheck-dev-key-change-in-prod")

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Initialize database tables (PostgreSQL / SQLite)
try:
    init_db()
except Exception as e:
    app.logger.warning(f"Database initialization warning: {e}")

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Simple in-memory caches to save API quota on repeated lookups
HEALTH_CACHE = {}
NEWS_CACHE = {}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


# ── STAGE 1: Vision (fastest — returns product info + ingredients) ──────────
@app.route("/analyze/vision", methods=["POST"])
def analyze_vision():
    """Extract brand, product, ingredients, and nutrition from the image.
    Returns instantly so the frontend can render product info right away."""

    if "image" not in request.files:
        return jsonify({"error": "No image file uploaded."}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No file selected."}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Upload JPG, PNG, or WebP."}), 400

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(UPLOAD_FOLDER, job_id)
    os.makedirs(job_dir, exist_ok=True)

    filename = secure_filename(file.filename)
    image_path = os.path.join(job_dir, filename)
    file.save(image_path)

    try:
        vision_result = extract(image_path)

        if "error" in vision_result:
            return jsonify({"error": vision_result["error"]}), 422

        brand = vision_result.get("brand", "Unknown")
        product_name = vision_result.get("product_name", "Unknown Product")
        ingredients_text = vision_result.get("ingredients_text", "")
        nutrition_facts = vision_result.get("nutrition_facts")

        # Quick Open Food Facts lookup (non-blocking, timeout 4s)
        off_match = None
        try:
            off_match = search(product_name, brand)
        except Exception:
            pass

        # Merge nutrition
        merged_nutrition = nutrition_facts or {}
        if off_match and off_match.get("nutriments"):
            for k, v in off_match["nutriments"].items():
                if v is not None and (k not in merged_nutrition or merged_nutrition.get(k) is None):
                    merged_nutrition[k] = v

        return jsonify({
            "brand": brand,
            "product_name": product_name,
            "ingredients_text": ingredients_text,
            "nutrition_facts": merged_nutrition,
            "off_match": off_match,
            "data_source": vision_result.get("data_source", "image"),
        })

    except Exception as e:
        app.logger.error(f"Vision error: {e}", exc_info=True)
        return jsonify({"error": f"Vision analysis failed: {str(e)}"}), 500

    finally:
        try:
            shutil.rmtree(job_dir)
        except OSError:
            pass


# ── STAGE 2: Health scoring (called after vision returns) ───────────────────
@app.route("/analyze/health", methods=["POST"])
def analyze_health():
    """Score ingredients + nutrition. Called with JSON body from frontend."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided."}), 400

    ingredients_text = data.get("ingredients_text", "")
    nutrition_facts = data.get("nutrition_facts", {})
    product_name = data.get("product_name", "")

    cache_key = f"{product_name.strip().lower()}:{ingredients_text.strip().lower()[:150]}"
    if cache_key in HEALTH_CACHE:
        app.logger.info(f"Serving health analysis from cache for key: {cache_key}")
        return jsonify(HEALTH_CACHE[cache_key])

    try:
        health_result = analyze(ingredients_text, nutrition_facts, product_name)
        if health_result.get("score") is not None:
            HEALTH_CACHE[cache_key] = health_result
        return jsonify(health_result)
    except Exception as e:
        app.logger.error(f"Health analysis error: {e}", exc_info=True)
        return jsonify({
            "score": None,
            "verdict": "unknown",
            "flags": [],
            "alternatives": [],
            "reasoning": f"Health analysis failed: {str(e)}"
        })


# ── STAGE 3: News (independent, called in parallel with health) ─────────────
@app.route("/news")
def news_endpoint():
    """Fetch corporate transparency news for a brand."""
    brand = request.args.get("brand", "").strip()
    if not brand:
        return jsonify({"articles": []}), 200

    brand_key = brand.lower()
    if brand_key in NEWS_CACHE:
        return jsonify({"articles": NEWS_CACHE[brand_key]})

    try:
        articles = get_news(brand)
        articles_list = articles or []
        if articles_list:
            NEWS_CACHE[brand_key] = articles_list
        return jsonify({"articles": articles_list})
    except Exception:
        return jsonify({"articles": []})


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "version": "v1.2.3-fast-lite"})


@app.route("/debug/models")
def debug_models():
    """Diagnostic endpoint to inspect active models and key validity."""
    import google.generativeai as genai
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return jsonify({"error": "No GEMINI_API_KEY set in environment."}), 500

    masked_key = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 10 else "too_short"
    try:
        genai.configure(api_key=api_key)
        available = []
        for m in genai.list_models():
            methods = getattr(m, "supported_generation_methods", []) or []
            if "generateContent" in methods:
                available.append(m.name)
        return jsonify({
            "key_preview": masked_key,
            "total_supported": len(available),
            "models": available,
        })
    except Exception as e:
        return jsonify({
            "key_preview": masked_key,
            "error": str(e),
            "error_type": type(e).__name__,
        }), 500


FEEDBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedbacks.json")

DEFAULT_FEEDBACKS = [
    {
        "id": "fb-1",
        "name": "Aarav Sharma",
        "rating": 5,
        "message": "Incredible accuracy! It detected excess sodium and hidden maltodextrin in instant noodles and suggested healthier millet noodles.",
        "date": "Today, 10:45 AM"
    },
    {
        "id": "fb-2",
        "name": "Priya Patel",
        "rating": 5,
        "message": "As a mother of two, the additive red flags help me check breakfast cereals in seconds. Love the clean design and dark theme!",
        "date": "Yesterday, 4:20 PM"
    },
    {
        "id": "fb-3",
        "name": "Rohan Deshmukh",
        "rating": 4,
        "message": "Quick and accurate label scanning. The camera option works smoothly on mobile. Super helpful app!",
        "date": "2 days ago"
    }
]


def load_feedbacks():
    if not os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_FEEDBACKS, f, indent=2)
            return list(DEFAULT_FEEDBACKS)
        except Exception:
            return list(DEFAULT_FEEDBACKS)
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else list(DEFAULT_FEEDBACKS)
    except Exception:
        return list(DEFAULT_FEEDBACKS)


def save_feedbacks(feedbacks):
    try:
        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(feedbacks, f, indent=2)
    except Exception as e:
        app.logger.error(f"Failed to save feedbacks: {e}")


# ── USER AUTHENTICATION & SESSION ROUTES ───────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (data.get("username") or data.get("user_id") or "").strip().lower()
    password = (data.get("password") or "").strip()
    name = (data.get("name") or username).strip()
    is_dev = bool(data.get("is_developer"))

    if not username or not password:
        return jsonify({"error": "User ID and Password are required."}), 400

    role = "developer" if is_dev else "user"
    user, err = register_account(username, password, name=name, role=role)
    if err:
        return jsonify({"error": err}), 400

    session["user_id"] = user["user_id"]
    session["name"] = user["name"]
    session["role"] = user["role"]

    return jsonify({"status": "success", "user": user})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    username = (data.get("username") or data.get("user_id") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "Please provide both User ID and Password."}), 400

    user, err = authenticate_account(username, password)
    if err:
        return jsonify({"error": err}), 401

    session["user_id"] = user["user_id"]
    session["name"] = user["name"]
    session["role"] = user["role"]

    return jsonify({"status": "success", "user": user})


@app.route("/api/auth/logout", methods=["POST", "GET"])
def auth_logout():
    session.clear()
    return jsonify({"status": "success", "message": "Logged out successfully."})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"authenticated": False})
    account = get_account(user_id)
    if not account:
        session.clear()
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "user_id": account["user_id"],
        "name": account["name"],
        "role": account.get("role", "user"),
        "is_developer": account.get("role") == "developer",
    })


# ── COMMUNITY FEEDBACK & EDIT ROUTES ────────────────────────────────────────

@app.route("/api/feedback", methods=["GET", "POST"])
def handle_feedback():
    user_id = session.get("user_id")
    is_dev = session.get("role") == "developer"

    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        if not data:
            return jsonify({"error": "No data received."}), 400

        user_id_val = user_id or "anonymous"
        name = (data.get("name") or session.get("name") or "User").strip()
        message = (data.get("message") or "").strip()
        rating = data.get("rating", 5)

        if not message:
            return jsonify({"error": "Please write your feedback message."}), 400

        fb_id = f"fb-{str(uuid.uuid4())[:8]}"
        saved_fb = save_feedback_db(fb_id, user_id_val, name, rating, message)

        # All signed-in users can see all feedbacks
        if user_id:
            feedbacks = get_all_feedbacks()
            for f in feedbacks:
                f["can_edit"] = is_dev or (f.get("user_id") == user_id)
        else:
            saved_fb["can_edit"] = False
            feedbacks = [saved_fb]

        return jsonify({
            "status": "success",
            "feedback": saved_fb,
            "feedbacks": feedbacks,
            "is_developer": is_dev,
            "is_signed_in": bool(user_id)
        })

    else:
        if user_id:
            # All signed-in users see all feedback from everyone
            feedbacks = get_all_feedbacks()
            for f in feedbacks:
                f["can_edit"] = is_dev or (f.get("user_id") == user_id)

            return jsonify({
                "feedbacks": feedbacks,
                "is_developer": is_dev,
                "is_signed_in": True,
                "role": session.get("role", "user")
            })
        else:
            # Not logged in yet - prompt to sign in to see community feedback
            return jsonify({
                "feedbacks": [],
                "is_developer": False,
                "is_signed_in": False,
                "prompt_login": True
            })


@app.route("/api/feedback/<fb_id>/edit", methods=["POST", "PUT"])
@app.route("/api/feedback/<fb_id>", methods=["PUT"])
def edit_feedback_endpoint(fb_id):
    """Allow signed-in users to edit their own feedback (or developers to edit any feedback)."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please sign in to edit your feedback.", "auth_required": True}), 401

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Feedback message cannot be empty."}), 400

    rating = data.get("rating", 5)
    is_dev = session.get("role") == "developer"

    updated_fb, err = update_feedback_db(fb_id, user_id, rating, message, is_dev=is_dev)
    if err:
        return jsonify({"error": err}), 403

    # Return updated community feedback list
    feedbacks = get_all_feedbacks()
    for f in feedbacks:
        f["can_edit"] = is_dev or (f.get("user_id") == user_id)

    return jsonify({
        "status": "success",
        "message": "Feedback updated successfully!",
        "feedback": updated_fb,
        "feedbacks": feedbacks
    })


# ── PRIVATE DIET PLANNER & CALORIE TRACKER ROUTES ──────────────────────────

@app.route("/api/diet/profile", methods=["GET", "POST"])
def diet_profile():
    """Retrieve or save user profile for personalized diet planning."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to access your personal profile.", "auth_required": True}), 401

    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        if not data:
            return jsonify({"error": "No profile data provided."}), 400

        data["user_id"] = user_id

        # Calculate BMR and TDEE based on inputs
        stats = calculate_bmr_and_tdee(
            age=data.get("age"),
            gender=data.get("gender"),
            height_cm=data.get("height_cm"),
            weight_kg=data.get("weight_kg"),
            activity_level=data.get("activity_level", "moderate"),
            goal=data.get("goal", "maintain")
        )

        data["target_calories"] = int(data.get("target_calories") or stats["target_calories"])
        data["target_protein_g"] = int(data.get("target_protein_g") or stats["target_protein_g"])
        data["target_carbs_g"] = int(data.get("target_carbs_g") or stats["target_carbs_g"])
        data["target_fat_g"] = int(data.get("target_fat_g") or stats["target_fat_g"])

        try:
            profile = save_user_profile(data)
            return jsonify({"status": "success", "profile": profile, "stats": stats})
        except Exception as e:
            app.logger.error(f"Error saving profile: {e}", exc_info=True)
            return jsonify({"error": f"Failed to save profile: {str(e)}"}), 500

    else:
        profile = get_user_profile(user_id)
        if not profile:
            profile = {
                "user_id": user_id,
                "name": session.get("name") or user_id,
            }
        stats = calculate_bmr_and_tdee(
            age=profile.get("age"),
            gender=profile.get("gender"),
            height_cm=profile.get("height_cm"),
            weight_kg=profile.get("weight_kg"),
            activity_level=profile.get("activity_level"),
            goal=profile.get("goal")
        )
        return jsonify({"profile": profile, "stats": stats})


@app.route("/api/diet/plan/generate", methods=["POST"])
def generate_plan_endpoint():
    """Generate a personalized daily diet plan using Groq Cloud API (Meta-Llama-3.1-8B-Instruct)."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to generate your diet plan.", "auth_required": True}), 401

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    current_profile = get_user_profile(user_id) or {}
    for k, v in data.items():
        if v is not None and v != "":
            current_profile[k] = v

    current_profile["user_id"] = user_id
    if not current_profile.get("name"):
        current_profile["name"] = session.get("name") or user_id

    # Save latest profile preferences
    try:
        saved_profile = save_user_profile(current_profile)
    except Exception as e:
        app.logger.warning(f"Profile update warning: {e}")
        saved_profile = current_profile

    # Generate plan with Groq Cloud Meta-Llama-3.1-8B-Instruct
    try:
        plan_content = generate_diet_plan(saved_profile)
        plan_id = f"dp-{str(uuid.uuid4())[:8]}"
        plan_name = plan_content.get("plan_title") or f"{saved_profile.get('goal', 'Personalized').title()} Diet Plan"
        target_calories = plan_content.get("target_calories", saved_profile.get("target_calories", 2000))

        # Save to database (Aiven PostgreSQL or SQLite)
        save_diet_plan(
            plan_id=plan_id,
            user_id=user_id,
            plan_name=plan_name,
            target_calories=target_calories,
            plan_data=plan_content
        )

        return jsonify({
            "status": "success",
            "plan_id": plan_id,
            "plan": plan_content,
            "profile": saved_profile
        })
    except Exception as e:
        app.logger.error(f"Error generating diet plan: {e}", exc_info=True)
        return jsonify({"error": f"Failed to generate diet plan: {str(e)}"}), 500


@app.route("/api/diet/suggest_meal", methods=["POST"])
def suggest_meal_endpoint():
    """Suggest 3 meal options using Groq Meta-Llama-3.1-8B based on preferences & remaining calories."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to get meal suggestions.", "auth_required": True}), 401

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    meal_type = data.get("meal_type", "Lunch")
    craving = data.get("craving", "")

    # Target calories: either explicit or based on remaining calories today
    target_calories = data.get("target_calories")
    if not target_calories:
        today_summary = get_daily_calorie_summary(user_id)
        remaining = today_summary.get("calories_remaining", 500)
        target_calories = min(800, max(250, int(remaining)))

    profile = get_user_profile(user_id) or {}
    res = suggest_quick_meal(profile, meal_type=meal_type, target_calories=target_calories, craving=craving)
    return jsonify({"status": "success", "result": res})


@app.route("/api/diet/plans", methods=["GET"])
def get_user_plans():
    """Fetch history of generated diet plans for the logged in user."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to view your diet history.", "auth_required": True}), 401

    try:
        plans = get_user_diet_plans(user_id, limit=10)
        return jsonify({"plans": plans})
    except Exception as e:
        app.logger.error(f"Error fetching diet plans: {e}", exc_info=True)
        return jsonify({"plans": [], "error": str(e)})


@app.route("/api/diet/plan/<plan_id>", methods=["GET"])
def get_single_plan(plan_id):
    """Fetch a specific diet plan by plan_id ensuring user owns it."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to view this plan.", "auth_required": True}), 401

    try:
        plan = get_diet_plan(plan_id)
        if not plan:
            return jsonify({"error": "Diet plan not found."}), 404
        if plan.get("user_id") != user_id and session.get("role") != "developer":
            return jsonify({"error": "Access denied to this diet plan."}), 403
        return jsonify({"plan": plan})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calorie/log", methods=["POST"])
def log_calorie_meal():
    """Log a meal consumed by the user with calorie and macronutrient breakdown."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to track calories.", "auth_required": True}), 401

    data = request.get_json(silent=True) or request.form.to_dict()
    if not data:
        return jsonify({"error": "No meal data provided."}), 400

    meal_type = data.get("meal_type", "Breakfast").strip()
    food_item = data.get("food_item", "").strip()

    if not food_item:
        return jsonify({"error": "Please enter the food item name."}), 400

    try:
        calories = float(data.get("calories", 0))
    except (ValueError, TypeError):
        calories = 0.0

    portion = data.get("portion", "").strip() or "1 serving"
    protein_g = float(data.get("protein_g") or 0)
    carbs_g = float(data.get("carbs_g") or 0)
    fat_g = float(data.get("fat_g") or 0)
    log_date = data.get("log_date") or datetime.now().strftime("%Y-%m-%d")

    log_id = f"cal-{str(uuid.uuid4())[:8]}"

    try:
        logged = log_meal(
            log_id=log_id,
            user_id=user_id,
            log_date=log_date,
            meal_type=meal_type,
            food_item=food_item,
            portion=portion,
            calories=calories,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g
        )
        summary = get_daily_calorie_summary(user_id, log_date)
        return jsonify({
            "status": "success",
            "logged_item": logged,
            "daily_summary": summary
        })
    except Exception as e:
        app.logger.error(f"Error logging calorie item: {e}", exc_info=True)
        return jsonify({"error": f"Failed to log meal: {str(e)}"}), 500


@app.route("/api/calorie/today", methods=["GET"])
def get_today_calories():
    """Retrieve today's calorie and macronutrient progress for the logged in user."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to view calorie tracking.", "auth_required": True}), 401

    target_date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")

    try:
        summary = get_daily_calorie_summary(user_id, target_date)
        return jsonify(summary)
    except Exception as e:
        app.logger.error(f"Error fetching calorie summary: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/calorie/log/<log_id>", methods=["DELETE"])
def delete_calorie_log(log_id):
    """Delete a logged meal item."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in.", "auth_required": True}), 401

    try:
        success = delete_meal_log(log_id, user_id)
        summary = get_daily_calorie_summary(user_id)
        return jsonify({"status": "success" if success else "not_found", "daily_summary": summary})
    except Exception as e:
        app.logger.error(f"Error deleting calorie item: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum size is 5MB."}), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "An internal error occurred. Please try again."}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
