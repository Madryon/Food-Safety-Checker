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


load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB
app.secret_key = os.getenv("SECRET_KEY", "foodcheck-dev-key-change-in-prod")

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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


@app.route("/api/feedback", methods=["GET", "POST"])
def handle_feedback():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        if not data:
            return jsonify({"error": "No data received."}), 400

        name = (data.get("name") or "").strip()
        message = (data.get("message") or "").strip()
        rating = data.get("rating", 5)
        try:
            rating = max(1, min(5, int(rating)))
        except (ValueError, TypeError):
            rating = 5

        if not name:
            return jsonify({"error": "Please enter your name."}), 400
        if not message:
            return jsonify({"error": "Please write your feedback message."}), 400

        feedbacks = load_feedbacks()
        new_fb = {
            "id": f"fb-{str(uuid.uuid4())[:8]}",
            "name": name,
            "rating": rating,
            "message": message,
            "date": datetime.now().strftime("%b %d, %I:%M %p")
        }
        feedbacks.insert(0, new_fb)
        save_feedbacks(feedbacks)
        return jsonify({"status": "success", "feedback": new_fb, "feedbacks": feedbacks})
    else:
        return jsonify({"feedbacks": load_feedbacks()})


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum size is 5MB."}), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "An internal error occurred. Please try again."}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
