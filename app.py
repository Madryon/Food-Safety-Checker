import os
import uuid
import shutil
import json
import time
from datetime import datetime, timedelta

from flask import (
    Flask,
    request,
    render_template,
    jsonify,
    session,
    redirect,
    url_for,
    flash,
    abort,
)
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from flask_login import current_user, login_user, logout_user

from extensions import db, migrate, login_manager, csrf, limiter
from models import (
    User,
    AdminInviteKey,
    Scan,
    ActivityLog,
    UserSession,
    ErrorLog,
    AppSetting,
    LoginEvent,
)
from cli import register_cli_commands
from auth import auth_bp
from admin import admin_bp
from services.vision_extractor import extract
from services.product_lookup import search
from services.health_analyzer import analyze
from services.news_fetcher import get_news
from services.activity_tracker import (
    log_activity,
    log_scan,
    log_error,
    log_login_event,
    check_daily_scan_limit,
    get_client_ip,
)
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
    update_feedback_db,
    delete_feedback_db,
)
from services.diet_generator import generate_diet_plan, calculate_bmr_and_tdee, suggest_quick_meal


load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB upload limit
app.secret_key = os.getenv("SECRET_KEY", "foodcheck-dev-secret-key-change-in-prod-xyz789")

# ── DATABASE CONFIGURATION (SQLite default / PostgreSQL override) ───────────
db_url = (
    os.getenv("DATABASE_URL")
    or os.getenv("AIVEN_DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or ""
).strip()

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if db_url.startswith("postgresql://"):
    if "sslmode=" not in db_url:
        sep = "&" if "?" in db_url else "?"
        db_url += f"{sep}sslmode=require"
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    # Local SQLite default in instance/foodcheck.db
    os.makedirs(app.instance_path, exist_ok=True)
    sqlite_path = os.path.join(app.instance_path, "foodcheck.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ── SECURE COOKIE & SESSION POLICIES ───────────────────────────────────────
is_production = os.getenv("FLASK_ENV") == "production" or os.getenv("RENDER") is not None
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = is_production
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# ── INITIALIZE EXTENSIONS ──────────────────────────────────────────────────
db.init_app(app)
migrate.init_app(app, db)
login_manager.init_app(app)
csrf.init_app(app)
limiter.init_app(app)

# ── REGISTER BLUEPRINTS & CLI ──────────────────────────────────────────────
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
register_cli_commands(app)

# Ensure database tables exist (both SQLAlchemy models & legacy raw connection pool)
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        app.logger.warning(f"SQLAlchemy table creation warning: {e}")
    try:
        init_db()
    except Exception as e:
        app.logger.warning(f"Legacy database initialization warning: {e}")

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
HEALTH_CACHE = {}
NEWS_CACHE = {}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ── MIDDLEWARE: MAINTENANCE MODE & SESSION EXPIRY CHECK ─────────────────────
@app.before_request
def check_maintenance_and_session():
    # 1. Maintenance Mode Enforcement
    # Admins and static assets are exempt from maintenance screen
    path = request.path
    if not path.startswith("/static") and not path.startswith("/admin") and not path.startswith("/login"):
        try:
            maint_active = AppSetting.get_setting("maintenance_mode", "false") == "true"
            if maint_active:
                if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
                    return render_template("maintenance.html"), 503
        except Exception:
            pass

    # 2. Session Revocation & Last Seen Tracking
    if current_user.is_authenticated:
        current_user.last_seen_at = datetime.utcnow()
        auth_token = session.get("auth_token")
        if auth_token:
            token_hash = UserSession.hash_token(auth_token)
            user_sess = UserSession.query.filter_by(session_token_hash=token_hash).first()
            if user_sess and user_sess.revoked:
                logout_user()
                session.clear()
                flash("Your session was terminated by an administrator.", "warning")
                return redirect(url_for("auth.login"))
            elif user_sess:
                user_sess.last_seen_at = datetime.utcnow()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


# ── MIDDLEWARE: SECURITY HEADERS ────────────────────────────────────────────
@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "frame-src 'self' https://www.google.com/recaptcha/ https://recaptcha.google.com/recaptcha/; "
        "img-src 'self' data: blob: https://images.openfoodfacts.org; "
        "connect-src 'self' https://world.openfoodfacts.org; "
        "frame-ancestors 'none';"
    )
    return response


# ── CONTEXT PROCESSORS: ANNOUNCEMENT BANNER & RECAPTCHA KEY ────────────────
@app.context_processor
def inject_global_banner():
    try:
        banner = AppSetting.get_setting("announcement_banner", "")
    except Exception:
        banner = ""
    return dict(
        announcement_banner=banner,
        recaptcha_site_key=os.getenv("RECAPTCHA_SITE_KEY", ""),
    )


# ── PUBLIC ROUTES ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    log_activity(action="page_view", details={"page": "index"})
    return render_template("index.html")


# ── STAGE 1: Vision (Gemini Multimodal OCR & Product Identification) ─────────
@app.route("/analyze/vision", methods=["POST"])
@limiter.limit("20 per minute")
def analyze_vision():
    """Extract brand, product, ingredients, and nutrition from the image."""
    start_time = time.time()

    # Quota limit check
    allowed, remaining, limit = check_daily_scan_limit()
    if not allowed:
        return jsonify({
            "error": f"Daily scan quota reached ({limit} scans/day). Please try again tomorrow or sign in."
        }), 429

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

    image_size = os.path.getsize(image_path)
    image_mime = file.mimetype

    try:
        vision_result = extract(image_path)

        if "error" in vision_result:
            duration_ms = int((time.time() - start_time) * 1000)
            log_scan(
                source="vision",
                response_time_ms=duration_ms,
                image_size_bytes=image_size,
                image_mime_type=image_mime,
                success=False,
                error_message=vision_result["error"],
            )
            log_error("gemini", vision_result["error"], path=request.path)
            return jsonify({"error": vision_result["error"]}), 422

        brand = vision_result.get("brand", "Unknown")
        product_name = vision_result.get("product_name", "Unknown Product")
        ingredients_text = vision_result.get("ingredients_text", "")
        nutrition_facts = vision_result.get("nutrition_facts")

        # Open Food Facts lookup
        off_match = None
        try:
            off_match = search(product_name, brand)
        except Exception as off_err:
            log_error("open_food_facts", str(off_err), path=request.path)

        # Merge nutrition
        merged_nutrition = nutrition_facts or {}
        if off_match and off_match.get("nutriments"):
            for k, v in off_match["nutriments"].items():
                if v is not None and (k not in merged_nutrition or merged_nutrition.get(k) is None):
                    merged_nutrition[k] = v

        duration_ms = int((time.time() - start_time) * 1000)

        # Log scan in database
        scan_id = log_scan(
            product_name=product_name,
            brand=brand,
            source="vision",
            gemini_used=True,
            response_time_ms=duration_ms,
            image_size_bytes=image_size,
            image_mime_type=image_mime,
            success=True,
        )
        log_activity(
            action="analyze_vision",
            details={"brand": brand, "product": product_name, "scan_id": scan_id},
        )

        return jsonify({
            "brand": brand,
            "product_name": product_name,
            "ingredients_text": ingredients_text,
            "nutrition_facts": merged_nutrition,
            "off_match": off_match,
            "data_source": vision_result.get("data_source", "image"),
            "scan_id": scan_id,
        })

    except Exception as e:
        app.logger.error(f"Vision error: {e}", exc_info=True)
        log_error("gemini", str(e), path=request.path)
        return jsonify({"error": f"Vision analysis failed: {str(e)}"}), 500

    finally:
        try:
            shutil.rmtree(job_dir)
        except OSError:
            pass


# ── STAGE 2: Health Scoring (Gemini Text Assessment) ────────────────────────
@app.route("/analyze/health", methods=["POST"])
@limiter.limit("30 per minute")
def analyze_health():
    """Score ingredients + nutrition. Called with JSON body from frontend."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided."}), 400

    ingredients_text = data.get("ingredients_text", "")
    nutrition_facts = data.get("nutrition_facts", {})
    product_name = data.get("product_name", "")
    scan_id = data.get("scan_id")

    cache_key = f"{product_name.strip().lower()}:{ingredients_text.strip().lower()[:150]}"
    if cache_key in HEALTH_CACHE:
        return jsonify(HEALTH_CACHE[cache_key])

    try:
        health_result = analyze(ingredients_text, nutrition_facts, product_name)
        if health_result.get("score") is not None:
            HEALTH_CACHE[cache_key] = health_result

            # Update existing scan record if scan_id provided
            if scan_id:
                try:
                    s = Scan.query.get(int(scan_id))
                    if s:
                        s.health_verdict = health_result.get("verdict")
                        s.health_score = health_result.get("score")
                        flags = [f.get("type") for f in health_result.get("flags", []) if isinstance(f, dict)]
                        s.key_flags = json.dumps(flags)
                        db.session.commit()
                except Exception:
                    pass

        log_activity(
            action="analyze_health",
            details={
                "product": product_name,
                "score": health_result.get("score"),
                "verdict": health_result.get("verdict"),
            },
        )
        return jsonify(health_result)
    except Exception as e:
        app.logger.error(f"Health analysis error: {e}", exc_info=True)
        log_error("gemini", str(e), path=request.path)
        return jsonify({
            "score": None,
            "verdict": "unknown",
            "flags": [],
            "alternatives": [],
            "reasoning": f"Health analysis failed: {str(e)}"
        })


# ── STAGE 3: News (Corporate Transparency & FSSAI Notices) ──────────────────
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
        log_activity(action="news_fetch", details={"brand": brand, "count": len(articles_list)})
        return jsonify({"articles": articles_list})
    except Exception as e:
        log_error("news", str(e), path=request.path)
        return jsonify({"articles": []})


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "version": "v2.0.0-admin-secured"})


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


# ── COMMUNITY FEEDBACK & EDIT ROUTES ────────────────────────────────────────

# ── COMMUNITY FEEDBACK & EDIT ROUTES ────────────────────────────────────────

@app.route("/api/feedback", methods=["GET", "POST"])
def handle_feedback():
    user_id = session.get("user_id")
    is_admin = session.get("role") in ("admin",) or (current_user.is_authenticated and current_user.is_admin)
    is_dev = session.get("role") in ("developer", "admin") or is_admin

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

        if user_id:
            feedbacks = get_all_feedbacks()
            for f in feedbacks:
                f["can_edit"] = is_dev or (f.get("user_id") == user_id)
                f["can_delete"] = is_admin
        else:
            saved_fb["can_edit"] = False
            saved_fb["can_delete"] = False
            feedbacks = [saved_fb]

        log_activity(action="submit_feedback", details={"rating": rating, "fb_id": fb_id})

        return jsonify({
            "status": "success",
            "feedback": saved_fb,
            "feedbacks": feedbacks,
            "is_developer": is_dev,
            "is_admin": is_admin,
            "is_signed_in": bool(user_id),
        })

    else:
        if user_id:
            feedbacks = get_all_feedbacks()
            for f in feedbacks:
                f["can_edit"] = is_dev or (f.get("user_id") == user_id)
                f["can_delete"] = is_admin

            return jsonify({
                "feedbacks": feedbacks,
                "is_developer": is_dev,
                "is_admin": is_admin,
                "is_signed_in": True,
                "role": session.get("role", "user"),
            })
        else:
            return jsonify({
                "feedbacks": [],
                "is_developer": False,
                "is_admin": False,
                "is_signed_in": False,
                "prompt_login": True,
            })


@app.route("/api/feedback/<fb_id>/edit", methods=["POST", "PUT"])
@app.route("/api/feedback/<fb_id>", methods=["PUT"])
def edit_feedback_endpoint(fb_id):
    """Allow signed-in users to edit their own feedback."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please sign in to edit your feedback.", "auth_required": True}), 401

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Feedback message cannot be empty."}), 400

    rating = data.get("rating", 5)
    is_admin = session.get("role") in ("admin",) or (current_user.is_authenticated and current_user.is_admin)
    is_dev = session.get("role") in ("developer", "admin") or is_admin

    updated_fb, err = update_feedback_db(fb_id, user_id, rating, message, is_dev=is_dev)
    if err:
        return jsonify({"error": err}), 403

    feedbacks = get_all_feedbacks()
    for f in feedbacks:
        f["can_edit"] = is_dev or (f.get("user_id") == user_id)
        f["can_delete"] = is_admin

    log_activity(action="edit_feedback", details={"fb_id": fb_id})

    return jsonify({
        "status": "success",
        "message": "Feedback updated successfully!",
        "feedback": updated_fb,
        "feedbacks": feedbacks,
    })


@app.route("/api/feedback/<fb_id>", methods=["DELETE"])
def delete_feedback_endpoint(fb_id):
    """Allow admins (or review owners) to delete a community review."""
    user_id = session.get("user_id")
    is_admin = session.get("role") in ("admin",) or (current_user.is_authenticated and current_user.is_admin)

    if not is_admin and not user_id:
        return jsonify({"error": "Admin access required to delete reviews.", "auth_required": True}), 403

    success = delete_feedback_db(fb_id, user_id=user_id, is_admin=is_admin)
    if not success:
        return jsonify({"error": "Feedback not found or permission denied."}), 404

    log_activity(
        action="delete_feedback",
        details={"fb_id": fb_id, "by_admin": is_admin},
        user_id=current_user.id if current_user.is_authenticated else None,
    )

    feedbacks = get_all_feedbacks()
    for f in feedbacks:
        f["can_edit"] = is_admin or (f.get("user_id") == user_id)
        f["can_delete"] = is_admin

    return jsonify({
        "status": "success",
        "message": "Review deleted successfully.",
        "feedbacks": feedbacks,
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

        stats = calculate_bmr_and_tdee(
            age=data.get("age"),
            gender=data.get("gender"),
            height_cm=data.get("height_cm"),
            weight_kg=data.get("weight_kg"),
            activity_level=data.get("activity_level", "moderate"),
            goal=data.get("goal", "maintain"),
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
            goal=profile.get("goal"),
        )
        return jsonify({"profile": profile, "stats": stats})


@app.route("/api/diet/plan/generate", methods=["POST"])
def generate_plan_endpoint():
    """Generate a personalized daily diet plan using Groq Cloud API."""
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

    try:
        saved_profile = save_user_profile(current_profile)
    except Exception as e:
        app.logger.warning(f"Profile update warning: {e}")
        saved_profile = current_profile

    try:
        plan_content = generate_diet_plan(saved_profile)
        plan_id = f"dp-{str(uuid.uuid4())[:8]}"
        plan_name = plan_content.get("plan_title") or f"{saved_profile.get('goal', 'Personalized').title()} Diet Plan"
        target_calories = plan_content.get("target_calories", saved_profile.get("target_calories", 2000))

        save_diet_plan(
            plan_id=plan_id,
            user_id=user_id,
            plan_name=plan_name,
            target_calories=target_calories,
            plan_data=plan_content,
        )

        log_activity(action="generate_diet_plan", details={"plan_id": plan_id})

        return jsonify({
            "status": "success",
            "plan_id": plan_id,
            "plan": plan_content,
            "profile": saved_profile,
        })
    except Exception as e:
        app.logger.error(f"Error generating diet plan: {e}", exc_info=True)
        return jsonify({"error": f"Failed to generate diet plan: {str(e)}"}), 500


@app.route("/api/diet/suggest_meal", methods=["POST"])
def suggest_meal_endpoint():
    """Suggest 3 meal options based on preferences & remaining calories."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to get meal suggestions.", "auth_required": True}), 401

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    meal_type = data.get("meal_type", "Lunch")
    craving = data.get("craving", "")

    target_calories = data.get("target_calories")
    if not target_calories:
        today_summary = get_daily_calorie_summary(user_id)
        remaining = today_summary.get("calories_remaining", 500)
        target_calories = min(800, max(250, int(remaining)))

    profile = get_user_profile(user_id) or {}
    res = suggest_quick_meal(profile, meal_type=meal_type, target_calories=target_calories, craving=craving)
    log_activity(action="suggest_meal", details={"meal_type": meal_type})
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
        if plan.get("user_id") != user_id and session.get("role") not in ("developer", "admin"):
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
            fat_g=fat_g,
        )
        summary = get_daily_calorie_summary(user_id, log_date)
        return jsonify({
            "status": "success",
            "logged_item": logged,
            "daily_summary": summary,
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


# ── EXISTING /api/auth/* AJAX ENDPOINTS (SYNCHRONIZED WITH FLASK-LOGIN) ────

@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("10 per minute")
def auth_register():
    try:
        # Check registration enabled
        if AppSetting.get_setting("registration_enabled", "true") == "false":
            return jsonify({"error": "Public registration is currently disabled by administrator."}), 403

        data = request.get_json(silent=True) or request.form.to_dict() or {}
        raw_username = (data.get("username") or data.get("user_id") or "").strip()
        username = raw_username.lower().replace(" ", "_")
        password = (data.get("password") or "").strip()
        name = (data.get("name") or raw_username or username).strip()
        is_admin_req = bool(data.get("is_admin") or data.get("register_as_admin"))
        invite_key = (data.get("invite_key") or data.get("admin_key") or "").strip()

        if not username or not password:
            return jsonify({"error": "User ID and Password are required."}), 400
        if len(username) < 2:
            return jsonify({"error": "User ID must be at least 2 characters long."}), 400
        if len(password) < 4:
            return jsonify({"error": "Password must be at least 4 characters long."}), 400

        # Handle Admin Registration with invite key
        if is_admin_req or invite_key:
            if AdminInviteKey.verify_key(invite_key):
                role = "admin"
            else:
                return jsonify({"error": "Invalid Admin Invite Key. Enter the assigned admin key to register as administrator."}), 400
        else:
            role = "user"

        # Check existing User in SQLAlchemy
        existing = User.query.filter_by(username=username).first()
        if existing:
            return jsonify({"error": "User ID already exists. Please choose a different ID or Sign In."}), 400

        # Save to legacy layer
        user_legacy, err = register_account(username, password, name=name, role=role)
        if err:
            return jsonify({"error": err}), 400

        # Sync into SQLAlchemy User
        sql_user = User(
            username=username,
            name=name,
            role=role,
            is_active=True,
        )
        sql_user.set_password(password)
        db.session.add(sql_user)
        db.session.commit()

        # Log in via Flask-Login and session
        login_user(sql_user)
        session["user_id"] = sql_user.username
        session["name"] = sql_user.name
        session["role"] = sql_user.role
        if role == "admin":
            session["admin_last_active"] = datetime.utcnow().timestamp()

        event_name = "admin_register" if role == "admin" else "register"
        log_login_event(event_name, user_id=sql_user.id, email_attempted=username, success=True)
        log_activity(f"user_{event_name}", details={"username": username, "role": role}, user_id=sql_user.id)

        return jsonify({
            "status": "success",
            "user": {
                "user_id": sql_user.username,
                "name": sql_user.name,
                "role": sql_user.role,
                "is_admin": sql_user.is_admin,
            }
        })
    except Exception as e:
        app.logger.error(f"Error during registration endpoint: {e}", exc_info=True)
        return jsonify({"error": f"Failed to register: {str(e)}"}), 500


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def auth_login():
    try:
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        raw_username = (data.get("username") or data.get("user_id") or "").strip()
        username = raw_username.lower().replace(" ", "_")
        password = (data.get("password") or "").strip()
        login_as_admin = bool(data.get("is_admin") or data.get("login_as_admin"))

        if not username or not password:
            return jsonify({"error": "Please provide both User ID and Password."}), 400

        # Check Master Admin Password
        is_master_pass = User.is_master_admin_password(password)

        if login_as_admin or is_master_pass:
            if is_master_pass:
                # User provided the Master Admin Password!
                user = User.query.filter_by(username=username).first()
                if not user:
                    # Auto-create admin user if doesn't exist yet
                    user = User(
                        username=username,
                        name=raw_username.capitalize() or username,
                        role="admin",
                        is_active=True,
                    )
                    user.set_password(password)
                    db.session.add(user)
                    db.session.commit()
                    try:
                        register_account(username, password, name=user.name, role="admin")
                    except Exception:
                        pass
                else:
                    # Promote/ensure role is admin
                    user.role = "admin"
                    user.is_active = True
                    user.reset_failed_logins()
                    db.session.commit()

                user.record_successful_login(ip=get_client_ip())
                db.session.commit()

                login_user(user)
                session["user_id"] = user.username
                session["name"] = user.name
                session["role"] = user.role
                session["admin_last_active"] = datetime.utcnow().timestamp()

                log_login_event("admin_login", user_id=user.id, email_attempted=username, success=True)
                log_activity("admin_login", details={"username": user.username}, user_id=user.id)

                return jsonify({
                    "status": "success",
                    "user": {
                        "user_id": user.username,
                        "name": user.name,
                        "role": user.role,
                        "is_admin": True,
                    }
                })

            # If user checked 'Log in as Admin' but password is NOT master password:
            # Check if user already exists, is an admin, and their personal password matches
            user = User.query.filter_by(username=username).first()
            if user and user.is_admin and user.check_password(password):
                if not user.is_active:
                    return jsonify({"error": "This admin account is currently disabled."}), 403
                if user.is_locked:
                    minutes = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
                    return jsonify({"error": f"Account temporarily locked. Retry in {minutes} minutes."}), 429

                user.record_successful_login(ip=get_client_ip())
                db.session.commit()
                login_user(user)
                session["user_id"] = user.username
                session["name"] = user.name
                session["role"] = user.role
                session["admin_last_active"] = datetime.utcnow().timestamp()

                log_login_event("admin_login", user_id=user.id, email_attempted=username, success=True)
                log_activity("admin_login", details={"username": user.username}, user_id=user.id)

                return jsonify({
                    "status": "success",
                    "user": {
                        "user_id": user.username,
                        "name": user.name,
                        "role": user.role,
                        "is_admin": True,
                    }
                })

            log_login_event("failed_login", email_attempted=username, success=False)
            return jsonify({"error": "Invalid Administrator credentials or Master Password."}), 401

        # Standard User Login
        user = User.query.filter_by(username=username).first()
        if not user:
            # Fallback check in legacy users_auth table
            user_legacy, err = authenticate_account(username, password)
            if err or not user_legacy:
                log_login_event("failed_login", email_attempted=username, success=False)
                return jsonify({"error": "Invalid User ID or Password."}), 401

            # Migrate / mirror user into SQLAlchemy User
            user = User(
                username=user_legacy["user_id"],
                name=user_legacy["name"],
                role=user_legacy.get("role", "user"),
                is_active=True,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

        # Check disabled
        if not user.is_active:
            log_login_event("failed_login", user_id=user.id, email_attempted=username, success=False)
            return jsonify({"error": "This account is currently disabled."}), 403

        # Check locked
        if user.is_locked:
            minutes = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
            log_login_event("locked", user_id=user.id, email_attempted=username, success=False)
            return jsonify({"error": f"Account temporarily locked. Retry in {minutes} minutes."}), 429

        # Validate password
        if not user.check_password(password):
            was_locked = user.record_failed_login()
            db.session.commit()
            log_login_event("failed_login", user_id=user.id, email_attempted=username, success=False)
            if was_locked:
                return jsonify({"error": "Too many failed attempts. Account locked for 15 minutes."}), 429
            return jsonify({"error": "Invalid User ID or Password."}), 401

        # Success
        user.record_successful_login(ip=get_client_ip())
        db.session.commit()

        login_user(user)
        session["user_id"] = user.username
        session["name"] = user.name
        session["role"] = user.role
        if user.is_admin:
            session["admin_last_active"] = datetime.utcnow().timestamp()

        log_login_event("login", user_id=user.id, email_attempted=username, success=True)
        log_activity("user_login", details={"username": user.username}, user_id=user.id)

        return jsonify({
            "status": "success",
            "user": {
                "user_id": user.username,
                "name": user.name,
                "role": user.role,
                "is_admin": user.is_admin,
            }
        })
    except Exception as e:
        app.logger.error(f"Error during login endpoint: {e}", exc_info=True)
        return jsonify({"error": f"Failed to log in: {str(e)}"}), 500



@app.route("/api/auth/logout", methods=["POST", "GET"])
def auth_logout():
    if current_user.is_authenticated:
        log_login_event("logout", user_id=current_user.id, success=True)
        logout_user()
    session.clear()
    return jsonify({"status": "success", "message": "Logged out successfully."})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    try:
        if current_user.is_authenticated:
            return jsonify({
                "authenticated": True,
                "user_id": current_user.username,
                "name": current_user.name,
                "role": current_user.role,
                "is_admin": current_user.is_admin,
                "is_developer": current_user.role in ("developer", "admin"),
            })

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
            "is_admin": account.get("role") == "admin",
            "is_developer": account.get("role") in ("developer", "admin"),
        })
    except Exception as e:
        app.logger.error(f"Error in auth_me endpoint: {e}", exc_info=True)
        return jsonify({"authenticated": False, "error": str(e)})


# Exempt legacy JSON API endpoints from CSRF
for _api_func in [
    auth_register,
    auth_login,
    auth_logout,
    handle_feedback,
    edit_feedback_endpoint,
    diet_profile,
    generate_plan_endpoint,
    suggest_meal_endpoint,
    get_user_plans,
    get_single_plan,
    log_calorie_meal,
    get_today_calories,
    delete_calorie_log,
    delete_feedback_endpoint,
]:
    csrf.exempt(_api_func)


# ── ERROR HANDLERS ──────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Resource not found."}), 404


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum size is 5MB."}), 413


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"error": f"Rate limit exceeded: {e.description}"}), 429


@app.errorhandler(500)
def server_error(e):
    log_error("app", str(e), path=request.path)
    return jsonify({"error": "An internal error occurred. Please try again."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)

