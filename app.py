import os
import re
import secrets
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
    make_response,
    send_from_directory,
)
from dotenv import load_dotenv
load_dotenv()

from services.captcha import get_captcha_provider, captcha_manager
from services.recaptcha import verify_captcha_token
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
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
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB upload limit
def _get_or_create_secret_key():
    env_key = (os.environ.get("SECRET_KEY") or "").strip()
    if env_key:
        return env_key
    inst_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
    key_file = os.path.join(inst_dir, ".secret_key")
    try:
        os.makedirs(inst_dir, exist_ok=True)
        if os.path.exists(key_file):
            with open(key_file, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    return k
        new_key = secrets.token_hex(32)
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(new_key)
        return new_key
    except Exception:
        return "foodcheck-production-stable-fallback-secret-2026"

app.secret_key = _get_or_create_secret_key()

# ── DATABASE CONFIGURATION (SQLite default / PostgreSQL override) ───────────
db_url = (
    os.getenv("DATABASE_URL")
    or os.getenv("AIVEN_DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or ""
).strip()

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

is_pg = False
if db_url.startswith("postgresql://") or db_url.startswith("postgresql+"):
    if "sslmode=" not in db_url:
        sep = "&" if "?" in db_url else "?"
        db_url += f"{sep}sslmode=require"

    # Fast DNS pre-flight check: ensure external host exists before connecting
    host = ""
    try:
        from urllib.parse import urlparse
        import socket
        parsed_url = urlparse(db_url)
        host = parsed_url.hostname or ""
        if host:
            socket.gethostbyname(host)
        is_pg = True
    except Exception as dns_err:
        app.logger.warning(
            f"⚠️ PostgreSQL host '{host}' could not be resolved ({dns_err}). "
            "Falling back to local SQLite database so the application continues to run without crashing."
        )
        is_pg = False

    if is_pg:
        # SQLAlchemy 2.0+ defaults 'postgresql://' to psycopg (v3).
        # If psycopg is missing but psycopg2 is present, use postgresql+psycopg2://
        if db_url.startswith("postgresql://"):
            try:
                import psycopg  # noqa: F401
            except ImportError:
                try:
                    import psycopg2  # noqa: F401
                    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
                except ImportError:
                    pass
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_recycle": 280,
            "pool_size": 3,
            "max_overflow": 2,
            "pool_timeout": 15,
        }

if not is_pg:
    # Local SQLite default in instance/foodcheck.db
    os.makedirs(app.instance_path, exist_ok=True)
    sqlite_path = os.path.join(app.instance_path, "foodcheck.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {"timeout": 20},
    }

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


@login_manager.user_loader
def load_user(user_id):
    try:
        from models import User
        try:
            return User.query.get(int(user_id))
        except (ValueError, TypeError):
            return User.query.filter_by(username=str(user_id)).first()
    except Exception:
        return None
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
_last_auto_purge_time = 0
_maint_cache = {"val": False, "ts": 0}

@app.route("/healthz", methods=["GET"])
def healthz():
    """Ultra-lightweight ping endpoint for Render health checks without database overhead."""
    return "ok", 200

def _run_daily_purge_if_needed():
    global _last_auto_purge_time
    now = time.time()
    if now - _last_auto_purge_time < 86400:
        return
    _last_auto_purge_time = now
    try:
        cutoff = datetime.utcnow() - timedelta(days=90)
        ActivityLog.query.filter(ActivityLog.created_at < cutoff).delete()
        LoginEvent.query.filter(LoginEvent.created_at < cutoff).delete()
        ErrorLog.query.filter(ErrorLog.created_at < cutoff).delete()
        from models import AdminAuditLog
        AdminAuditLog.query.filter(AdminAuditLog.created_at < cutoff).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()

@app.before_request
def check_maintenance_and_session():
    path = request.path
    # 0. Fast-path: Skip DB operations entirely for static files and health checks
    if (
        request.endpoint == "static"
        or path.startswith("/static")
        or path in ("/favicon.ico", "/favicon.svg", "/healthz", "/robots.txt")
    ):
        return

    # 1. Lightweight daily purge (runs at most once every 24 hours)
    _run_daily_purge_if_needed()

    # 2. Maintenance Mode Enforcement (cached in-memory for 5s to avoid DB queries on every request)
    global _maint_cache
    now = time.time()
    cache_ttl = 0 if app.config.get("TESTING") else 5
    if now - _maint_cache["ts"] > cache_ttl:
        try:
            val = AppSetting.get_setting("maintenance_mode", "false")
            _maint_cache["val"] = (val == "true")
            _maint_cache["ts"] = now
        except Exception:
            _maint_cache["val"] = False
            _maint_cache["ts"] = now

    if _maint_cache["val"] and not path.startswith("/admin") and not path.startswith("/login"):
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            return render_template("maintenance.html"), 503

    # 3. Session Revocation & Last Seen Tracking (throttled to at most once per 60s per user)
    if current_user.is_authenticated:
        sess_last_sync = session.get("_last_seen_sync", 0)
        if now - sess_last_sync > 60:
            session["_last_seen_sync"] = now
            try:
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
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/ https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "frame-src 'self' https://www.google.com/recaptcha/ https://recaptcha.google.com/recaptcha/ https://challenges.cloudflare.com; "
        "media-src 'self' data: blob:; "
        "img-src 'self' data: blob: https://images.openfoodfacts.org; "
        "connect-src 'self' https://world.openfoodfacts.org https://www.google.com https://challenges.cloudflare.com; "
        "frame-ancestors 'none';"
    )
    return response


# ── CONTEXT PROCESSORS: ANNOUNCEMENT BANNER & RECAPTCHA KEY ────────────────
@app.context_processor
def inject_global_banner():
    banner = ""
    try:
        banner = AppSetting.get_setting("announcement_banner", "")
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        banner = ""
    provider = get_captcha_provider()
    return dict(
        announcement_banner=banner,
        recaptcha_site_key=os.getenv("RECAPTCHA_SITE_KEY", "").strip(),
        turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY", ""),
        captcha_provider=provider.provider_name,
    )


# ── PUBLIC ROUTES ───────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(app.root_path, "static"), "favicon.svg", mimetype="image/svg+xml")


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

    except (requests.exceptions.Timeout, TimeoutError) as te:
        app.logger.warning(f"Vision analysis timeout: {te}")
        log_error("gemini_timeout", str(te), path=request.path)
        return jsonify({"error": "Vision analysis timed out. The server is under heavy load or the image was too large. Please retry with a smaller photo."}), 504
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
    except (requests.exceptions.Timeout, TimeoutError) as te:
        app.logger.warning(f"News fetch timed out for brand '{brand}': {te}")
        log_error("news_timeout", str(te), path=request.path)
        return jsonify({"articles": [], "notice": "News search timed out gracefully."}), 200
    except Exception as e:
        log_error("news", str(e), path=request.path)
        return jsonify({"articles": []}), 200


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
    role = session.get("role", "user")
    is_admin = (role == "admin")
    is_dev = (role in ("developer", "admin"))

    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        if not data:
            return jsonify({"error": "No data received."}), 400

        # Mandatory authentication: user must sign in or register to submit feedback
        if not user_id:
            return jsonify({
                "error": "Please sign in or create an account to submit your feedback.",
                "auth_required": True
            }), 401

        name = (data.get("name") or session.get("name") or (current_user.name if current_user.is_authenticated else "") or user_id).strip()
        message = (data.get("message") or "").strip()
        rating = data.get("rating", 5)

        if not message:
            return jsonify({"error": "Please write your feedback message."}), 400

        fb_id = f"fb-{str(uuid.uuid4())[:8]}"
        saved_fb = save_feedback_db(fb_id, user_id, name, rating, message)

        feedbacks = get_all_feedbacks()
        for f in feedbacks:
            f["can_edit"] = is_dev or (f.get("user_id") == user_id)
            f["can_delete"] = is_admin

        log_activity(action="submit_feedback", details={"rating": rating, "fb_id": fb_id})

        return jsonify({
            "status": "success",
            "feedback": saved_fb,
            "feedbacks": feedbacks,
            "is_developer": is_dev,
            "is_admin": is_admin,
            "is_signed_in": True,
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
    curr_is_admin = False
    if current_user and getattr(current_user, "is_authenticated", False):
        if getattr(current_user, "username", None) == user_id:
            curr_is_admin = getattr(current_user, "is_admin", False)

    is_admin = session.get("role") in ("admin",) or curr_is_admin
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
    curr_is_admin = False
    if current_user and getattr(current_user, "is_authenticated", False):
        if getattr(current_user, "username", None) == user_id:
            curr_is_admin = getattr(current_user, "is_admin", False)
    is_admin = session.get("role") in ("admin",) or curr_is_admin

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

        log_activity(action="diet_plan_generated", details={"plan_id": plan_id})
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
    log_activity(action="meal_suggest", details={"meal_type": meal_type})
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
        log_activity("calorie_log_added", details={"meal_type": meal_type})
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


# ── CAPTCHA ENDPOINTS (CUSTOM / TURNSTILE / RECAPTCHA) ─────────────────────

@app.route("/api/captcha/config", methods=["GET"])
def api_captcha_config():
    provider = get_captcha_provider()
    return jsonify(provider.get_client_config())

@app.route("/api/captcha/generate", methods=["GET"])
@app.route("/api/captcha/challenge", methods=["GET"])
def api_captcha_generate():
    ip = get_client_ip()
    ch_data, error_msg, status_code = captcha_manager.create_challenge(ip)
    if error_msg:
        return jsonify({"success": False, "message": error_msg}), status_code

    # Store challenge info in Flask session with 2-minute expiry
    session["captcha_challenge_id"] = ch_data["challengeId"]
    session["captcha_expires_at"] = time.time() + 120
    session["captcha_label"] = ch_data.get("label", "")

    return jsonify({"success": True, **ch_data})

api_captcha_challenge = api_captcha_generate

@app.route("/api/captcha/image/<challenge_id>/<int:img_idx>", methods=["GET"])
def api_captcha_image(challenge_id, img_idx):
    img_bytes = captcha_manager.get_image_bytes(challenge_id, img_idx)
    if not img_bytes:
        abort(404)
    resp = make_response(img_bytes)
    resp.headers["Content-Type"] = "image/jpeg"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/captcha/audio/<challenge_id>", methods=["GET"])
def api_captcha_audio(challenge_id):
    audio_bytes = captcha_manager.get_audio_bytes(challenge_id)
    if not audio_bytes:
        abort(404)
    resp = make_response(audio_bytes)
    resp.headers["Content-Type"] = "audio/wav"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/captcha/verify", methods=["POST"])
def api_captcha_verify():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    challenge_id = data.get("challengeId") or data.get("challenge_id") or session.get("captcha_challenge_id")
    if not challenge_id:
        return jsonify({"success": False, "message": "Missing challengeId."}), 400

    selected_indexes = data.get("selectedIndexes")
    if selected_indexes is None:
        selected_indexes = data.get("selected_indexes")
    audio_answer = data.get("audioAnswer") or data.get("audio_answer")
    ip = get_client_ip()

    # Session expiry check if session has captcha_expires_at
    sess_exp = session.get("captcha_expires_at")
    if sess_exp and time.time() > sess_exp:
        session.pop("captcha_challenge_id", None)
        session.pop("captcha_expires_at", None)
        return jsonify({"success": False, "message": "CAPTCHA challenge expired (2 minute validity). Please refresh."}), 400

    success, token, err_msg, status_code = captcha_manager.verify_challenge(
        challenge_id=challenge_id,
        selected_indexes=selected_indexes,
        audio_answer=audio_answer,
        ip=ip
    )
    if not success:
        return jsonify({"success": False, "message": err_msg}), status_code

    # Store verified token in session
    session["captcha_verified_token"] = token
    session["captcha_verified_at"] = time.time()
    session.pop("captcha_challenge_id", None)
    session.pop("captcha_expires_at", None)

    return jsonify({"success": True, "token": token, "captchaToken": token})


# ── EXISTING /api/auth/* AJAX ENDPOINTS (SYNCHRONIZED WITH FLASK-LOGIN) ────

@app.route("/register", methods=["GET", "POST"])
def app_register():
    if request.method == "POST":
        return auth_register()
    return redirect(url_for("auth.register"))


@app.route("/login", methods=["GET", "POST"])
def app_login():
    if request.method == "POST":
        return auth_login()
    return redirect(url_for("auth.login"))

@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("10 per minute")
def auth_register():
    try:
        # Check registration enabled
        if AppSetting.get_setting("registration_enabled", "true") == "false":
            return jsonify({"error": "Public registration is currently disabled by administrator."}), 403

        data = request.get_json(silent=True) or request.form.to_dict() or {}

        # Honeypot check
        honeypot = (data.get("honeypot") or "").strip()
        if honeypot:
            return jsonify({"error": "Bot activity detected. Registration rejected."}), 400

        # CAPTCHA verification
        captcha_enabled = app.config.get("CAPTCHA_ENABLED", app.config.get("RECAPTCHA_ENABLED", not app.config.get("TESTING", False)))
        if captcha_enabled:
            token = (
                data.get("captcha_token")
                or data.get("captchaToken")
                or data.get("g-recaptcha-response")
                or data.get("cf-turnstile-response")
                or ""
            ).strip()
            session_token = session.get("captcha_verified_token")
            ip = get_client_ip()

            if not token and not session_token:
                session.pop("captcha_verified_token", None)
                session.pop("captcha_verified_at", None)
                return jsonify({"error": "CAPTCHA verification failed: Missing verification token. Please complete the image verification."}), 400

            valid = False
            msg = ""
            if session_token and (not token or token == session_token):
                sess_verified_at = session.get("captcha_verified_at", 0)
                if time.time() - sess_verified_at <= 300:
                    captcha_manager.validate_and_burn_token(session_token, ip)
                    valid = True
                    msg = "Verified via session token."
                else:
                    msg = "CAPTCHA session token expired."

            if not valid and token:
                valid, msg = verify_captcha_token(token, ip)

            # Invalidate session token immediately to prevent replay attacks
            session.pop("captcha_verified_token", None)
            session.pop("captcha_verified_at", None)
            session.pop("captcha_challenge_id", None)
            session.pop("captcha_expires_at", None)

            if not valid:
                return jsonify({"error": f"CAPTCHA verification failed: {msg}"}), 400

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
        if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            return jsonify({"error": "Password must be at least 8 characters long and contain both letters and numbers."}), 400

        # Handle Admin Registration with invite key or master password
        invite = None
        is_master_key = User.is_master_admin_password(invite_key)
        if is_master_key:
            role = "admin"
        elif is_admin_req or invite_key:
            invite = AdminInviteKey.verify_key(invite_key)
            if invite:
                role = "admin"
            else:
                return jsonify({"error": "Invalid Admin Invite Key. Enter a valid developer-issued invite key or master password to register as administrator."}), 400
        else:
            if AppSetting.get_setting("registration_enabled", "true") == "false":
                return jsonify({"error": "Public registration is currently disabled by administrator."}), 403
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

        # Mark single-use invite key as consumed
        if invite:
            invite.used_at = datetime.utcnow()
            invite.used_by_user_id = sql_user.id
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

        # Honeypot check
        honeypot = (data.get("honeypot") or "").strip()
        if honeypot:
            return jsonify({"error": "Bot activity detected. Login rejected."}), 400

        # CAPTCHA verification
        captcha_enabled = app.config.get("CAPTCHA_ENABLED", app.config.get("RECAPTCHA_ENABLED", not app.config.get("TESTING", False)))
        if captcha_enabled:
            token = (
                data.get("captcha_token")
                or data.get("captchaToken")
                or data.get("g-recaptcha-response")
                or data.get("cf-turnstile-response")
                or ""
            ).strip()
            session_token = session.get("captcha_verified_token")
            ip = get_client_ip()

            if not token and not session_token:
                session.pop("captcha_verified_token", None)
                session.pop("captcha_verified_at", None)
                return jsonify({"error": "CAPTCHA verification failed: Missing verification token. Please complete the image verification."}), 400

            valid = False
            msg = ""
            if session_token and (not token or token == session_token):
                sess_verified_at = session.get("captcha_verified_at", 0)
                if time.time() - sess_verified_at <= 300:
                    captcha_manager.validate_and_burn_token(session_token, ip)
                    valid = True
                    msg = "Verified via session token."
                else:
                    msg = "CAPTCHA session token expired."

            if not valid and token:
                valid, msg = verify_captcha_token(token, ip)

            # Invalidate session token immediately to prevent replay attacks
            session.pop("captcha_verified_token", None)
            session.pop("captcha_verified_at", None)
            session.pop("captcha_challenge_id", None)
            session.pop("captcha_expires_at", None)

            if not valid:
                return jsonify({"error": f"CAPTCHA verification failed: {msg}"}), 400

        raw_username = (data.get("username") or data.get("user_id") or "").strip()
        username = raw_username.lower().replace(" ", "_")
        password = (data.get("password") or "").strip()
        login_as_admin = bool(data.get("is_admin") or data.get("login_as_admin"))

        if not username or not password:
            return jsonify({"error": "Please provide both User ID and Password."}), 400

        # Master Admin Password check
        is_master = User.is_master_admin_password(password)
        if login_as_admin or is_master:
            if is_master:
                user = User.query.filter_by(username=username).first()
                if not user:
                    user = User(
                        username=username,
                        name=username.capitalize(),
                        role="admin",
                        is_active=True,
                    )
                    user.set_password(password)
                    db.session.add(user)
                    db.session.commit()
                    try:
                        register_account(username, password, name=username.capitalize(), role="admin")
                    except Exception:
                        pass
                else:
                    user.role = "admin"
                    user.is_active = True
                    user.reset_failed_logins()
                    db.session.commit()

                user.record_successful_login(ip=get_client_ip())
                db.session.commit()
                captcha_manager.record_successful_login(get_client_ip())
                login_user(user)
                session["user_id"] = user.username
                session["name"] = user.name
                session["role"] = user.role
                session["admin_last_active"] = datetime.utcnow().timestamp()

                log_login_event("admin_login", user_id=user.id, email_attempted=username, success=True)
                log_activity("admin_login", details={"username": user.username, "method": "master_key"}, user_id=user.id)

                return jsonify({
                    "status": "success",
                    "user": {
                        "user_id": user.username,
                        "name": user.name,
                        "role": user.role,
                        "is_admin": True,
                    }
                })

            user = User.query.filter_by(username=username).first()
            if not user or not user.is_admin:
                log_login_event("failed_login", email_attempted=username, success=False)
                return jsonify({"error": "Invalid Administrator credentials or access denied."}), 401

            if not user.is_active:
                log_login_event("failed_login", user_id=user.id, email_attempted=username, success=False)
                return jsonify({"error": "This administrator account is disabled."}), 403

            if user.is_locked:
                minutes = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
                log_login_event("locked", user_id=user.id, email_attempted=username, success=False)
                return jsonify({"error": f"Account temporarily locked. Retry in {minutes} minutes."}), 429

            if not user.check_password(password):
                user.record_failed_login()
                db.session.commit()
                log_login_event("failed_login", user_id=user.id, email_attempted=username, success=False)
                return jsonify({"error": "Invalid Administrator credentials or access denied."}), 401

            user.record_successful_login(ip=get_client_ip())
            db.session.commit()
            captcha_manager.record_successful_login(get_client_ip())
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
                "is_admin": current_user.is_admin or current_user.role in ("admin", "developer"),
                "is_developer": current_user.role in ("developer", "admin"),
            })

        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"authenticated": False})
        account = get_account(user_id)
        if not account:
            session.clear()
            return jsonify({"authenticated": False})
        user_role = account.get("role", "user")
        return jsonify({
            "authenticated": True,
            "user_id": account["user_id"],
            "name": account["name"],
            "role": user_role,
            "is_admin": user_role in ("admin", "developer"),
            "is_developer": user_role in ("developer", "admin"),
        })
    except Exception as e:
        app.logger.error(f"Error in auth_me endpoint: {e}", exc_info=True)
        return jsonify({"authenticated": False, "error": str(e)})


@app.route("/api/activity/theme", methods=["POST"])
def log_theme_activity():
    data = request.get_json(silent=True) or {}
    theme = (data.get("theme") or "").strip()[:20]
    log_activity(action="theme_change", details={"theme": theme})
    return jsonify({"status": "ok"})


# Exempt legacy JSON API endpoints from CSRF
for _api_func in [
    auth_register,
    auth_login,
    auth_logout,
    analyze_vision,
    analyze_health,
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
    api_captcha_verify,
    app_register,
    app_login,
    log_theme_activity,
]:
    csrf.exempt(_api_func)


# ── ERROR HANDLERS ──────────────────────────────────────────────────────────

@app.errorhandler(400)
def bad_request(e):
    desc = getattr(e, "description", None) or str(e)
    if "400 Bad Request:" in desc:
        desc = desc.replace("400 Bad Request:", "").strip()
    return jsonify({"error": desc, "message": desc}), 400


@app.errorhandler(404)
def not_found(e):
    if request.accept_mimetypes.accept_html and not request.is_json and not request.path.startswith("/api/"):
        return redirect(url_for("index"))
    return jsonify({"error": "Resource not found."}), 404


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum size is 5MB."}), 413


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"error": f"Rate limit exceeded: {e.description}"}), 429


@app.errorhandler(500)
def server_error(e):
    app.logger.error(f"Internal server error on {request.path}: {e}", exc_info=True)
    try:
        db.session.rollback()
    except Exception:
        pass
    try:
        log_error("app", str(e), path=request.path)
    except Exception:
        pass
    if request.accept_mimetypes.accept_html and not request.is_json and not request.path.startswith("/api/"):
        try:
            return render_template("maintenance.html"), 500
        except Exception:
            pass
    return jsonify({"error": "An internal error occurred. Please try again."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)

