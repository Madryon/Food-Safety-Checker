import os
import uuid
import shutil

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

    try:
        health_result = analyze(ingredients_text, nutrition_facts, product_name)
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

    try:
        articles = get_news(brand)
        return jsonify({"articles": articles or []})
    except Exception:
        return jsonify({"articles": []})


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum size is 5MB."}), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "An internal error occurred. Please try again."}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
