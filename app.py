import os
import re
import secrets
import uuid
import shutil
import json
import time
import threading
import sqlite3
from datetime import datetime, timedelta

from sqlalchemy import event
from sqlalchemy.engine import Engine

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
from flask_compress import Compress
from concurrent.futures import ThreadPoolExecutor
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
from services.ai_provider import (
    get_ai_status_summary,
    get_gemini_api_keys,
    get_groq_api_keys,
)
from services.gemini_client import discover_available_models
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
Compress(app)
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

    # Fast DNS & TCP pre-flight check: ensure external host is reachable before connecting
    host = ""
    try:
        from urllib.parse import urlparse
        import socket
        parsed_url = urlparse(db_url)
        host = parsed_url.hostname or ""
        if host:
            socket.gethostbyname(host)
            port = parsed_url.port or 5432
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            conn_res = sock.connect_ex((host, port))
            sock.close()
            if conn_res != 0:
                raise ConnectionRefusedError(f"TCP connect to {host}:{port} failed (code {conn_res})")
        is_pg = True
    except Exception as dns_err:
        app.logger.warning(
            f"⚠️ PostgreSQL host '{host}' could not be reached ({dns_err}). "
            "Falling back to local SQLite database so the application and admin console continue to run without crashing."
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
            "pool_recycle": 300,
            "pool_size": 5,
            "max_overflow": 5,
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
# Configure SQLite WAL mode, normal synchronous, and busy timeout on connection
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            pass
        finally:
            cursor.close()


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

# ── Thread-Safe In-Memory TTL Cache (60s TTL) & Concurrency Thread Pool ─────
_CACHE_TTL = 60
_cache_lock = threading.Lock()
HEALTH_CACHE = {}
NEWS_CACHE = {}


def _cache_get(cache: dict, key: str):
    entry = cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    cache.pop(key, None)
    return None


def _cache_set(cache: dict, key: str, value):
    with _cache_lock:
        cache[key] = (time.monotonic(), value)
        if len(cache) > 300:
            oldest = sorted(cache, key=lambda k: cache[k][0])[:60]
            for k in oldest:
                cache.pop(k, None)


# Thread pool for parallel external API operations (Open Food Facts, News, Health scoring)
_io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fc_io")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ── MIDDLEWARE: MAINTENANCE MODE & SESSION EXPIRY CHECK ─────────────────────
_last_auto_purge_time = 0
_maint_cache = {"val": False, "ts": 0}

@app.route("/healthz", methods=["GET"])
def healthz():
    """Ultra-lightweight ping endpoint for Render health checks without database overhead."""
    return jsonify({"status": "ok"}), 200

def _run_daily_purge_if_needed():
    global _last_auto_purge_time
    now = time.time()
    if now - _last_auto_purge_time < 86400:
        return
    _last_auto_purge_time = now

    def _do_purge(app_obj):
        with app_obj.app_context():
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

    if app.config.get("TESTING"):
        _do_purge(app)
    else:
        _io_pool.submit(_do_purge, app)

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
                sync=False,
            )
            log_error("gemini", vision_result["error"], path=request.path)
            return jsonify({"error": vision_result["error"]}), 422

        brand = vision_result.get("brand", "Unknown")
        product_name = vision_result.get("product_name", "Unknown Product")
        ingredients_text = vision_result.get("ingredients_text", "")
        nutrition_facts = vision_result.get("nutrition_facts")

        # ── Parallel Network I/O via ThreadPoolExecutor ──────────────────────────
        # 1. Fetch Open Food Facts in background
        off_future = _io_pool.submit(search, product_name, brand)

        # 2. Concurrently pre-fetch brand news so /news responds immediately
        if brand and brand != "Unknown":
            _io_pool.submit(get_news, brand)

        # 3. Concurrently pre-warm health scoring cache so /analyze/health responds immediately
        if ingredients_text or nutrition_facts:
            _health_cache_key = f"{product_name.strip().lower()}:{ingredients_text.strip().lower()[:150]}"
            def _prewarm_health():
                try:
                    h_res = analyze(ingredients_text, nutrition_facts, product_name)
                    if h_res and h_res.get("score") is not None:
                        _cache_set(HEALTH_CACHE, _health_cache_key, h_res)
                except Exception:
                    pass
            _io_pool.submit(_prewarm_health)

        # Await Open Food Facts with strict 5s timeout & graceful fallback
        off_match = None
        try:
            off_match = off_future.result(timeout=5)
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
    cached_health = _cache_get(HEALTH_CACHE, cache_key)
    if cached_health:
        return jsonify(cached_health)

    try:
        health_result = analyze(ingredients_text, nutrition_facts, product_name)
        if health_result.get("score") is not None:
            _cache_set(HEALTH_CACHE, cache_key, health_result)

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
    cached_news = _cache_get(NEWS_CACHE, brand_key)
    if cached_news is not None:
        return jsonify({"articles": cached_news})

    try:
        articles = get_news(brand)
        articles_list = articles or []
        if articles_list:
            _cache_set(NEWS_CACHE, brand_key, articles_list)
        log_activity(action="news_fetch", details={"brand": brand, "count": len(articles_list)})
        return jsonify({"articles": articles_list})
    except (requests.exceptions.Timeout, TimeoutError) as te:
        app.logger.warning(f"News fetch timed out for brand '{brand}': {te}")
        log_error("news_timeout", str(te), path=request.path)
        return jsonify({"articles": [], "notice": "News search timed out gracefully."}), 200
    except Exception as e:
        log_error("news", str(e), path=request.path)
        return jsonify({"articles": []}), 200


# ── SEARCH FOOD SAFETY: 24-hour result cache ─────────────────────────────────
SEARCH_RESULT_CACHE: dict = {}
_SEARCH_RESULT_TTL = 86400  # 24 hours
_SEARCH_REJECTION_LOG: list = []  # Capped at 1000 entries
_SEARCH_LOG_LOCK = threading.Lock()


def _search_cache_get(key: str):
    entry = SEARCH_RESULT_CACHE.get(key)
    if entry and (time.monotonic() - entry[0]) < _SEARCH_RESULT_TTL:
        return entry[1]
    SEARCH_RESULT_CACHE.pop(key, None)
    return None


def _search_cache_set(key: str, value):
    with _cache_lock:
        SEARCH_RESULT_CACHE[key] = (time.monotonic(), value)
        if len(SEARCH_RESULT_CACHE) > 500:
            oldest = sorted(SEARCH_RESULT_CACHE, key=lambda k: SEARCH_RESULT_CACHE[k][0])[:100]
            for k in oldest:
                SEARCH_RESULT_CACHE.pop(k, None)


def _log_rejection(food: str, brand: str, variant: str, reason: str, ip: str):
    """Log rejected queries without personal data."""
    entry = {
        "ts": time.time(),
        "food_len": len(food),
        "brand_len": len(brand),
        "variant_len": len(variant),
        "reason": reason[:100],
        "ip_hash": str(hash(ip))[-8:],
    }
    with _SEARCH_LOG_LOCK:
        _SEARCH_REJECTION_LOG.append(entry)
        if len(_SEARCH_REJECTION_LOG) > 1000:
            del _SEARCH_REJECTION_LOG[:100]


# ── SEARCH FOOD SAFETY: Autocomplete Suggestions ─────────────────────────────
@app.route("/api/search/suggest")
@limiter.limit("60 per minute")
def search_suggest():
    """
    GET /api/search/suggest?type=food|brand|variant&q=...&food=...&brand=...
    Returns up to 8 autocomplete suggestions for any user (Guest or Logged-in).
    """
    from services.search_service import (
        get_food_suggestions,
        get_brand_suggestions,
        get_variant_suggestions,
    )
    suggest_type = request.args.get("type", "food").strip().lower()
    q = request.args.get("q", "").strip()
    food = request.args.get("food", "").strip()
    brand = request.args.get("brand", "").strip()

    q = q[:120]
    food = food[:120]
    brand = brand[:60]

    try:
        if suggest_type == "food" or not suggest_type:
            items = get_food_suggestions(q or food)
        elif suggest_type == "brand":
            items = get_brand_suggestions(q, food)
        elif suggest_type == "variant":
            items = get_variant_suggestions(brand, food, q)
        else:
            items = []
    except Exception as e:
        app.logger.warning(f"Search suggest failed ({suggest_type}): {e}")
        items = []

    return jsonify({"suggestions": items})


# ── SEARCH FOOD SAFETY: Unified Search & Analysis Pipeline ────────────────────
@app.route("/api/search-food", methods=["POST"])
@app.route("/api/search/analyze", methods=["POST"])
@app.route("/analyze-text", methods=["POST"])
@limiter.limit("30 per minute")
@csrf.exempt
def search_food():
    """
    POST /api/search-food or /api/search/analyze or /analyze-text
    Body: { "query": "<food_name>", "category": "...", "brand": "...", "variant": "..." }
    Accepts both free-form queries (e.g. 'Maggi 2-Minute Noodles') and structured wizard inputs.
    Open to ALL users (Guest or Logged-in).
    Returns unified report: { vision, health, news, safety_score, verdict, ... }
    """
    from services.search_guard import (
        validate_field,
        topic_guard,
        check_cooldown,
        record_rejection,
        record_success,
    )
    from services.search_service import get_search_report

    client_ip = get_client_ip()

    # 1. Cooldown check
    blocked, block_msg = check_cooldown(client_ip)
    if blocked:
        return jsonify({"error": block_msg}), 429

    # Accept JSON body, Form data, or query parameters
    data = request.get_json(silent=True) or request.form.to_dict() or request.args.to_dict() or {}
    if not data:
        return jsonify({"error": "Invalid request body. Expected JSON or form data."}), 400

    # 2. Honeypot check (bots fill this hidden field)
    if data.get("_hp", ""):
        app.logger.warning(f"Search honeypot triggered from {client_ip}")
        return jsonify({"error": "Invalid request."}), 400

    # Flexible parameter support (e.g. query/food/product_name/name, category, brand/company, variant/flavor)
    query = str(
        data.get("query")
        or data.get("food")
        or data.get("product_name")
        or data.get("product")
        or data.get("name")
        or ""
    ).strip()
    category = str(data.get("category") or "").strip()
    brand = str(
        data.get("brand") or data.get("company") or data.get("brand_name") or ""
    ).strip()
    variant = str(
        data.get("variant") or data.get("flavor") or ""
    ).strip()

    if not query:
        return jsonify({"error": "Please enter a food or product name to search."}), 400

    # 3. Field format validation (up to 120 chars)
    ok, err = validate_field(query[:120], "Food query")
    if not ok:
        _log_rejection(query, brand, variant, err, client_ip)
        record_rejection(client_ip)
        return jsonify({"error": err}), 400

    if brand and brand.lower() not in ("generic", "not sure", "any", "unknown", "not specified"):
        ok, err = validate_field(brand, "Brand")
        if not ok:
            _log_rejection(query, brand, variant, err, client_ip)
            record_rejection(client_ip)
            return jsonify({"error": err}), 400

    if variant and variant != "Not sure / Any variant":
        ok, err = validate_field(variant, "Variant")
        if not ok:
            _log_rejection(query, brand, variant, err, client_ip)
            record_rejection(client_ip)
            return jsonify({"error": err}), 400

    # 4. Topic guard (OFF + curated lists + classifier)
    guard_ok, guard_msg = topic_guard(query, brand)
    if not guard_ok:
        _log_rejection(query, brand, variant, guard_msg, client_ip)
        record_rejection(client_ip)
        return jsonify({"error": guard_msg}), 422

    # 5. 24-hour result cache
    cache_key = f"search::{query.lower()}::{brand.lower()}::{variant.lower()}"
    cached = _search_cache_get(cache_key)
    if cached:
        record_success(client_ip)
        log_activity(action="search_food_cached", details={"query": query, "brand": brand})
        return jsonify(cached)

    # 6. Build report via ThreadPoolExecutor parallel pipeline
    start_time = time.time()
    try:
        report = get_search_report(food=query, brand=brand, variant=variant, query=query)
        _search_cache_set(cache_key, report)
        record_success(client_ip)
        duration_ms = int((time.time() - start_time) * 1000)

        # 7. Log scan in database so it appears in Admin Console just like a photo scan
        scan_id = log_scan(
            product_name=report.get("product_name") or query,
            brand=report.get("brand") or brand or "Unknown",
            source="search",
            health_verdict=(report.get("health") or {}).get("verdict"),
            health_score=(report.get("health") or {}).get("score"),
            key_flags=[f.get("type") for f in (report.get("health") or {}).get("flags", []) if isinstance(f, dict)],
            gemini_used=True,
            response_time_ms=duration_ms,
            image_size_bytes=0,
            image_mime_type="text/plain",
            success=True,
            user_id=current_user.id if (current_user and current_user.is_authenticated) else None,
            ip=client_ip,
        )
        report["scan_id"] = scan_id
        if "vision" in report and isinstance(report["vision"], dict):
            report["vision"]["scan_id"] = scan_id

        log_activity(
            action="search_food",
            details={
                "query": query,
                "product": report.get("product_name"),
                "brand": report.get("brand"),
                "score": (report.get("health") or {}).get("score"),
                "scan_id": scan_id,
            },
        )
        return jsonify(report)
    except Exception as e:
        app.logger.error(f"Search food error: {e}", exc_info=True)
        log_error("search_food", str(e), path=request.path)
        return jsonify({
            "error": "Food analysis failed. Please try again.",
            "detail": str(e)[:200],
        }), 500


@app.route("/health")
def health_check():
    ai_status = get_ai_status_summary()
    return jsonify({
        "status": "ok",
        "version": "v2.1.0-multi-provider",
        "ai": ai_status,
    })


@app.route("/debug/models")
def debug_models():
    """Diagnostic endpoint to inspect active models and key validity across all providers."""
    ai_summary = get_ai_status_summary()
    gemini_keys = get_gemini_api_keys()

    key_details = []
    for idx, k in enumerate(gemini_keys):
        masked = k[:6] + "..." + k[-4:] if len(k) > 10 else f"key_{idx+1}"
        models = discover_available_models(k)
        key_details.append({
            "key_index": idx + 1,
            "preview": masked,
            "models_count": len(models),
            "priority_models": models[:4],
        })

    return jsonify({
        "ai_engine": ai_summary,
        "gemini_keys": key_details,
    })


# ── COMMUNITY FEEDBACK & EDIT ROUTES ────────────────────────────────────────

# ── COMMUNITY FEEDBACK & EDIT ROUTES ────────────────────────────────────────

@app.route("/api/feedback", methods=["GET", "POST"])
def handle_feedback():
    try:
        user_id = session.get("user_id")
        if not user_id and session.get("_user_id") and current_user.is_authenticated:
            user_id = current_user.username

        curr_uname = getattr(current_user, "username", None) if (current_user.is_authenticated and session.get("_user_id")) else None
        is_curr_admin = getattr(current_user, "is_admin", False) and (curr_uname == user_id)
        role = session.get("role") or (current_user.role if curr_uname == user_id else "user")
        is_admin = (role == "admin") or is_curr_admin
        is_dev = (role in ("developer", "admin")) or is_curr_admin

        if request.method == "POST":
            data = request.get_json(silent=True) or request.form.to_dict()
            if not data:
                return jsonify({"error": "No data received."}), 400

            # Mandatory authentication: user must sign in or register to submit feedback
            if not user_id or str(user_id).strip().lower() in ("anonymous", "none", ""):
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
            # GET: Community reviews are visible to all visitors and users
            feedbacks = get_all_feedbacks()
            for f in feedbacks:
                f["can_edit"] = bool(user_id and (is_dev or f.get("user_id") == user_id))
                f["can_delete"] = bool(user_id and is_admin)

            return jsonify({
                "feedbacks": feedbacks,
                "is_developer": is_dev,
                "is_admin": is_admin,
                "is_signed_in": bool(user_id),
                "role": role if user_id else "guest",
                "prompt_login": not bool(user_id),
            })
    except Exception as e:
        app.logger.error(f"Error in handle_feedback: {e}", exc_info=True)
        return jsonify({
            "feedbacks": get_all_feedbacks(),
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
        sess_role = str(session.get("role") or "").strip().lower()
        is_sess_adm = sess_role in ("admin", "developer")

        if current_user.is_authenticated:
            is_adm = is_sess_adm or bool(getattr(current_user, "is_admin", False) or getattr(current_user, "role", "") in ("admin", "developer"))
            return jsonify({
                "authenticated": True,
                "user_id": current_user.username,
                "name": current_user.name,
                "role": "admin" if is_adm else getattr(current_user, "role", "user"),
                "is_admin": is_adm,
                "is_developer": bool(getattr(current_user, "role", "") in ("developer", "admin") or is_sess_adm),
            })

        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"authenticated": False})
        account = get_account(user_id)
        if not account:
            if is_sess_adm:
                return jsonify({
                    "authenticated": True,
                    "user_id": str(user_id),
                    "name": session.get("name") or str(user_id).capitalize(),
                    "role": "admin",
                    "is_admin": True,
                    "is_developer": True,
                })
            session.clear()
            return jsonify({"authenticated": False})
        user_role = str(account.get("role", "user")).strip().lower()
        is_adm = is_sess_adm or (user_role in ("admin", "developer"))
        return jsonify({
            "authenticated": True,
            "user_id": account["user_id"],
            "name": account["name"],
            "role": "admin" if is_adm else user_role,
            "is_admin": is_adm,
            "is_developer": is_adm or (user_role in ("developer", "admin")),
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


@app.route("/admin")
def admin_root_redirect():
    return redirect(url_for("admin.dashboard"))


@app.errorhandler(404)
def not_found(e):
    if request.path in ("/admin", "/admin/"):
        return redirect(url_for("admin.dashboard"))
    if request.path.startswith("/admin/"):
        is_adm = (current_user.is_authenticated and (getattr(current_user, "is_admin", False) or getattr(current_user, "role", "") in ("admin", "developer"))) or session.get("role") in ("admin", "developer")
        if is_adm and request.path != "/admin/dashboard":
            return redirect(url_for("admin.dashboard"))
        elif not is_adm and request.path != "/admin/login":
            return redirect(url_for("admin.admin_login"))
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

    # If the user requested an admin route, NEVER return raw JSON!
    if request.path.startswith("/admin"):
        from flask import render_template_string
        return render_template_string("""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Recovery — FoodCheck</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', system-ui, sans-serif; background: #0b1117; color: #e8eef5; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; margin: 0; }
        .box { background: #121c27; border: 1px solid rgba(255,255,255,.1); border-radius: 16px; padding: 36px 32px; max-width: 480px; text-align: center; box-shadow: 0 12px 32px rgba(0,0,0,.4); }
        .icon { font-size: 44px; margin-bottom: 12px; }
        h1 { font-size: 22px; font-weight: 800; margin-bottom: 8px; }
        p { color: #8ea0b3; font-size: 14px; line-height: 1.6; margin-bottom: 24px; }
        .btn-row { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
        .btn { padding: 9px 18px; border-radius: 8px; font-weight: 700; font-size: 13.5px; text-decoration: none; cursor: pointer; border: 1px solid transparent; }
        .btn-p { background: #34d399; color: #04261b; }
        .btn-s { background: #182535; color: #e8eef5; border: 1px solid rgba(255,255,255,.12); }
    </style>
</head>
<body>
    <div class="box">
        <div class="icon">🛡️</div>
        <h1>Admin Console Operational Recovery</h1>
        <p>The Admin Console is operational. Click below to open your Dashboard or re-authenticate.</p>
        <div class="btn-row">
            <a href="/admin/dashboard" class="btn btn-p">📊 Open Dashboard</a>
            <a href="/admin/login" class="btn btn-s">🔐 Admin Sign In</a>
            <a href="/" class="btn btn-s">🌐 Main App</a>
        </div>
    </div>
</body>
</html>"""), 200

    if request.accept_mimetypes.accept_html and not request.is_json and not request.path.startswith("/api/"):
        try:
            return render_template("maintenance.html"), 500
        except Exception:
            pass
    return jsonify({"error": "An internal error occurred. Please try again."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)

