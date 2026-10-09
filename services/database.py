import os
import json
import logging
import sqlite3
import urllib.parse
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# Database URL from environment: AIVEN_DATABASE_URL, DATABASE_URL, or POSTGRES_URL
DB_URL = (
    os.getenv("AIVEN_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or ""
).strip()

USE_POSTGRES = False
pg_pool = None

# Normalize postgres:// to postgresql:// if needed for psycopg2
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

if DB_URL.startswith("postgresql://"):
    try:
        import psycopg2
        from psycopg2 import pool, extras

        # Ensure sslmode is present if connecting to cloud PG like Aiven
        if "sslmode=" not in DB_URL:
            separator = "&" if "?" in DB_URL else "?"
            DB_URL += f"{separator}sslmode=require"

        # Initialize connection pool
        pg_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DB_URL,
        )
        USE_POSTGRES = True
        logger.info("Connected to Aiven PostgreSQL database successfully.")
    except Exception as e:
        logger.warning(
            f"Could not connect to PostgreSQL ({e}). Falling back to local SQLite for diet data."
        )
        USE_POSTGRES = False
        pg_pool = None
else:
    logger.info("No PostgreSQL URL configured. Using local SQLite database for diet tracking.")

# Local SQLite fallback path
SQLITE_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "diet_tracker.db"
)


def get_or_create_pg_pool():
    """Retrieve existing PG pool or initialize a fresh pool if needed."""
    global pg_pool, USE_POSTGRES
    if not DB_URL or not DB_URL.startswith("postgresql://"):
        return None
    if pg_pool is not None:
        return pg_pool
    try:
        import psycopg2
        from psycopg2 import pool
        url = DB_URL
        if "sslmode=" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        pg_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10, dsn=url)
        USE_POSTGRES = True
        return pg_pool
    except Exception as e:
        logger.warning(f"Could not initialize PostgreSQL pool: {e}")
        return None


class DatabaseConnection:
    """Context manager to yield a DB connection and handle commits/rollbacks."""

    def __init__(self):
        self.conn = None
        self.is_pg = False

    def __enter__(self):
        pool_inst = get_or_create_pg_pool()
        if pool_inst:
            try:
                conn = pool_inst.getconn()
                # Verify connection is not closed
                if getattr(conn, "closed", 0) != 0:
                    try:
                        pool_inst.putconn(conn, close=True)
                    except Exception:
                        pass
                    conn = pool_inst.getconn()
                self.conn = conn
                self.is_pg = True
                return self.conn, True
            except Exception as e:
                logger.warning(f"PostgreSQL connection acquisition failed ({e}). Falling back to SQLite.")
                self.is_pg = False

        # SQLite fallback
        self.conn = sqlite3.connect(SQLITE_DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.is_pg = False
        return self.conn, False

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if self.is_pg:
                try:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                except Exception as e:
                    logger.warning(f"Error during PostgreSQL commit/rollback: {e}")

                try:
                    is_broken = exc_type is not None or getattr(self.conn, "closed", 0) != 0
                    if pg_pool:
                        pg_pool.putconn(self.conn, close=is_broken)
                except Exception as e:
                    logger.warning(f"Error putting PG connection back to pool: {e}")
            else:
                try:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                except Exception as e:
                    logger.warning(f"Error during SQLite commit/rollback: {e}")
                try:
                    self.conn.close()
                except Exception:
                    pass


def init_db():
    """Create necessary tables if they don't already exist."""
    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()

        if is_pg:
            # PostgreSQL Schema
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users_auth (
                    user_id VARCHAR(100) PRIMARY KEY,
                    password_hash VARCHAR(255) NOT NULL,
                    name VARCHAR(150),
                    role VARCHAR(20) DEFAULT 'user',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id VARCHAR(100) PRIMARY KEY,
                    name VARCHAR(150) NOT NULL,
                    age INT,
                    gender VARCHAR(20),
                    height_cm FLOAT,
                    weight_kg FLOAT,
                    activity_level VARCHAR(50),
                    goal VARCHAR(50),
                    diet_pref VARCHAR(50),
                    health_conditions TEXT,
                    target_calories INT,
                    target_protein_g INT,
                    target_carbs_g INT,
                    target_fat_g INT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS diet_plans (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(100) NOT NULL,
                    plan_name VARCHAR(200),
                    target_calories INT,
                    plan_data JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_user_diet FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS calorie_logs (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(100) NOT NULL,
                    log_date DATE NOT NULL,
                    meal_type VARCHAR(50) NOT NULL,
                    food_item VARCHAR(255) NOT NULL,
                    portion VARCHAR(100),
                    calories FLOAT NOT NULL,
                    protein_g FLOAT DEFAULT 0,
                    carbs_g FLOAT DEFAULT 0,
                    fat_g FLOAT DEFAULT 0,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_user_log FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(100) DEFAULT 'anonymous',
                    name VARCHAR(150) NOT NULL,
                    rating INT DEFAULT 5,
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_diet_plans_user ON diet_plans(user_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_calorie_logs_user_date ON calorie_logs(user_id, log_date);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_user ON feedbacks(user_id);")

        else:
            # SQLite Schema
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users_auth (
                    user_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    name TEXT,
                    role TEXT DEFAULT 'user',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    age INTEGER,
                    gender TEXT,
                    height_cm REAL,
                    weight_kg REAL,
                    activity_level TEXT,
                    goal TEXT,
                    diet_pref TEXT,
                    health_conditions TEXT,
                    target_calories INTEGER,
                    target_protein_g INTEGER,
                    target_carbs_g INTEGER,
                    target_fat_g INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS diet_plans (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_name TEXT,
                    target_calories INTEGER,
                    plan_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS calorie_logs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    log_date TEXT NOT NULL,
                    meal_type TEXT NOT NULL,
                    food_item TEXT NOT NULL,
                    portion TEXT,
                    calories REAL NOT NULL,
                    protein_g REAL DEFAULT 0,
                    carbs_g REAL DEFAULT 0,
                    fat_g REAL DEFAULT 0,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT DEFAULT 'anonymous',
                    name TEXT NOT NULL,
                    rating INTEGER DEFAULT 5,
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        # Ensure default developer account exists
        dev_pass = os.getenv("DEV_PASSWORD", "developer123")
        dev_hash = generate_password_hash(dev_pass)
        if is_pg:
            cursor.execute("""
                INSERT INTO users_auth (user_id, password_hash, name, role)
                VALUES ('developer', %s, 'App Developer', 'developer')
                ON CONFLICT (user_id) DO NOTHING;
            """, (dev_hash,))
        else:
            cursor.execute("""
                INSERT OR IGNORE INTO users_auth (user_id, password_hash, name, role)
                VALUES ('developer', ?, 'App Developer', 'developer');
            """, (dev_hash,))

        # Seed initial feedbacks from feedbacks.json if table is empty
        cursor.execute("SELECT COUNT(*) FROM feedbacks;")
        fb_count = cursor.fetchone()[0]
        if fb_count == 0:
            fb_json_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "feedbacks.json"
            )
            if os.path.exists(fb_json_path):
                try:
                    with open(fb_json_path, "r", encoding="utf-8") as f:
                        seed_items = json.load(f)
                    for item in seed_items:
                        fid = item.get("id") or f"fb-{datetime.now().timestamp()}"
                        fuser = "developer"
                        fname = item.get("name") or "User"
                        frating = int(item.get("rating") or 5)
                        fmsg = item.get("message") or ""
                        if is_pg:
                            cursor.execute("""
                                INSERT INTO feedbacks (id, user_id, name, rating, message)
                                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING;
                            """, (fid, fuser, fname, frating, fmsg))
                        else:
                            cursor.execute("""
                                INSERT OR IGNORE INTO feedbacks (id, user_id, name, rating, message)
                                VALUES (?, ?, ?, ?, ?);
                            """, (fid, fuser, fname, frating, fmsg))
                except Exception as ex:
                    logger.warning(f"Could not seed feedbacks: {ex}")

        cursor.close()
    logger.info("Database initialized successfully.")


# ─────────────────────────────────────────────────────────────
# User Profile Operations
# ─────────────────────────────────────────────────────────────

def save_user_profile(profile_data):
    """Insert or update user profile."""
    user_id = profile_data.get("user_id") or "default_user"
    name = profile_data.get("name") or "User"
    age = profile_data.get("age")
    gender = profile_data.get("gender")
    height_cm = profile_data.get("height_cm")
    weight_kg = profile_data.get("weight_kg")
    activity_level = profile_data.get("activity_level", "moderate")
    goal = profile_data.get("goal", "maintain")
    diet_pref = profile_data.get("diet_pref", "vegetarian")
    health_conditions = profile_data.get("health_conditions", "")
    target_calories = profile_data.get("target_calories", 2000)
    target_protein_g = profile_data.get("target_protein_g", 75)
    target_carbs_g = profile_data.get("target_carbs_g", 250)
    target_fat_g = profile_data.get("target_fat_g", 55)

    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            query = """
                INSERT INTO user_profiles (
                    user_id, name, age, gender, height_cm, weight_kg,
                    activity_level, goal, diet_pref, health_conditions,
                    target_calories, target_protein_g, target_carbs_g, target_fat_g,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    age = EXCLUDED.age,
                    gender = EXCLUDED.gender,
                    height_cm = EXCLUDED.height_cm,
                    weight_kg = EXCLUDED.weight_kg,
                    activity_level = EXCLUDED.activity_level,
                    goal = EXCLUDED.goal,
                    diet_pref = EXCLUDED.diet_pref,
                    health_conditions = EXCLUDED.health_conditions,
                    target_calories = EXCLUDED.target_calories,
                    target_protein_g = EXCLUDED.target_protein_g,
                    target_carbs_g = EXCLUDED.target_carbs_g,
                    target_fat_g = EXCLUDED.target_fat_g,
                    updated_at = CURRENT_TIMESTAMP;
            """
            cursor.execute(query, (
                user_id, name, age, gender, height_cm, weight_kg,
                activity_level, goal, diet_pref, health_conditions,
                target_calories, target_protein_g, target_carbs_g, target_fat_g
            ))
        else:
            query = """
                INSERT INTO user_profiles (
                    user_id, name, age, gender, height_cm, weight_kg,
                    activity_level, goal, diet_pref, health_conditions,
                    target_calories, target_protein_g, target_carbs_g, target_fat_g,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = excluded.name,
                    age = excluded.age,
                    gender = excluded.gender,
                    height_cm = excluded.height_cm,
                    weight_kg = excluded.weight_kg,
                    activity_level = excluded.activity_level,
                    goal = excluded.goal,
                    diet_pref = excluded.diet_pref,
                    health_conditions = excluded.health_conditions,
                    target_calories = excluded.target_calories,
                    target_protein_g = excluded.target_protein_g,
                    target_carbs_g = excluded.target_carbs_g,
                    target_fat_g = excluded.target_fat_g,
                    updated_at = CURRENT_TIMESTAMP;
            """
            cursor.execute(query, (
                user_id, name, age, gender, height_cm, weight_kg,
                activity_level, goal, diet_pref, health_conditions,
                target_calories, target_protein_g, target_carbs_g, target_fat_g
            ))
        cursor.close()

    return get_user_profile(user_id)


def get_user_profile(user_id):
    """Retrieve user profile by user_id."""
    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            from psycopg2.extras import RealDictCursor
            dict_cursor = conn.cursor(cursor_factory=RealDictCursor)
            dict_cursor.execute("SELECT * FROM user_profiles WHERE user_id = %s;", (user_id,))
            row = dict_cursor.fetchone()
            dict_cursor.close()
            if row:
                return dict(row)
        else:
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            cursor.close()
            if row:
                return dict(row)
    return None


# ─────────────────────────────────────────────────────────────
# Diet Plans Operations
# ─────────────────────────────────────────────────────────────

def save_diet_plan(plan_id, user_id, plan_name, target_calories, plan_data):
    """Store generated diet plan in database."""
    # Ensure profile exists for foreign key constraint in Postgres
    profile = get_user_profile(user_id)
    if not profile:
        save_user_profile({"user_id": user_id, "name": "User"})

    plan_data_str = json.dumps(plan_data) if isinstance(plan_data, dict) else str(plan_data)

    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            query = """
                INSERT INTO diet_plans (id, user_id, plan_name, target_calories, plan_data)
                VALUES (%s, %s, %s, %s, %s);
            """
            cursor.execute(query, (plan_id, user_id, plan_name, target_calories, plan_data_str))
        else:
            query = """
                INSERT INTO diet_plans (id, user_id, plan_name, target_calories, plan_data)
                VALUES (?, ?, ?, ?, ?);
            """
            cursor.execute(query, (plan_id, user_id, plan_name, target_calories, plan_data_str))
        cursor.close()

    return get_diet_plan(plan_id)


def get_diet_plan(plan_id):
    """Get diet plan by ID."""
    with DatabaseConnection() as (conn, is_pg):
        if is_pg:
            from psycopg2.extras import RealDictCursor
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM diet_plans WHERE id = %s;", (plan_id,))
            row = cursor.fetchone()
            cursor.close()
        else:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM diet_plans WHERE id = ?;", (plan_id,))
            row = cursor.fetchone()
            cursor.close()

        if row:
            res = dict(row)
            if isinstance(res.get("plan_data"), str):
                try:
                    res["plan_data"] = json.loads(res["plan_data"])
                except Exception:
                    pass
            return res
    return None


def get_user_diet_plans(user_id, limit=10):
    """Get history of diet plans generated for a user."""
    plans = []
    with DatabaseConnection() as (conn, is_pg):
        if is_pg:
            from psycopg2.extras import RealDictCursor
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "SELECT id, user_id, plan_name, target_calories, plan_data, created_at FROM diet_plans WHERE user_id = %s ORDER BY created_at DESC LIMIT %s;",
                (user_id, limit)
            )
            rows = cursor.fetchall()
            cursor.close()
        else:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, user_id, plan_name, target_calories, plan_data, created_at FROM diet_plans WHERE user_id = ? ORDER BY created_at DESC LIMIT ?;",
                (user_id, limit)
            )
            rows = cursor.fetchall()
            cursor.close()

        for r in rows:
            item = dict(r)
            if isinstance(item.get("plan_data"), str):
                try:
                    item["plan_data"] = json.loads(item["plan_data"])
                except Exception:
                    pass
            # Format timestamp string if object
            if isinstance(item.get("created_at"), datetime):
                item["created_at"] = item["created_at"].strftime("%Y-%m-%d %H:%M")
            plans.append(item)
    return plans


# ─────────────────────────────────────────────────────────────
# Calorie Logs Operations
# ─────────────────────────────────────────────────────────────

def log_meal(log_id, user_id, log_date, meal_type, food_item, portion, calories, protein_g=0, carbs_g=0, fat_g=0):
    """Log a consumed food item."""
    profile = get_user_profile(user_id)
    if not profile:
        save_user_profile({"user_id": user_id, "name": "User"})

    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            query = """
                INSERT INTO calorie_logs (
                    id, user_id, log_date, meal_type, food_item, portion,
                    calories, protein_g, carbs_g, fat_g
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """
            cursor.execute(query, (
                log_id, user_id, log_date, meal_type, food_item, portion,
                float(calories), float(protein_g), float(carbs_g), float(fat_g)
            ))
        else:
            query = """
                INSERT INTO calorie_logs (
                    id, user_id, log_date, meal_type, food_item, portion,
                    calories, protein_g, carbs_g, fat_g
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """
            cursor.execute(query, (
                log_id, user_id, str(log_date), meal_type, food_item, portion,
                float(calories), float(protein_g), float(carbs_g), float(fat_g)
            ))
        cursor.close()

    return {
        "id": log_id,
        "user_id": user_id,
        "log_date": str(log_date),
        "meal_type": meal_type,
        "food_item": food_item,
        "portion": portion,
        "calories": calories,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
    }


def get_daily_calorie_summary(user_id, target_date=None):
    """Retrieve all meals logged on a given day with aggregates."""
    if not target_date:
        target_date = date.today().isoformat()
    elif isinstance(target_date, (date, datetime)):
        target_date = target_date.strftime("%Y-%m-%d")

    meals = []
    total_cals = 0.0
    total_prot = 0.0
    total_carbs = 0.0
    total_fat = 0.0

    with DatabaseConnection() as (conn, is_pg):
        if is_pg:
            from psycopg2.extras import RealDictCursor
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT id, user_id, log_date, meal_type, food_item, portion,
                       calories, protein_g, carbs_g, fat_g, logged_at
                FROM calorie_logs
                WHERE user_id = %s AND log_date = %s
                ORDER BY logged_at ASC;
            """, (user_id, target_date))
            rows = cursor.fetchall()
            cursor.close()
        else:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, log_date, meal_type, food_item, portion,
                       calories, protein_g, carbs_g, fat_g, logged_at
                FROM calorie_logs
                WHERE user_id = ? AND log_date = ?
                ORDER BY logged_at ASC;
            """, (user_id, str(target_date)))
            rows = cursor.fetchall()
            cursor.close()

        for r in rows:
            item = dict(r)
            if isinstance(item.get("log_date"), (date, datetime)):
                item["log_date"] = item["log_date"].strftime("%Y-%m-%d")
            if isinstance(item.get("logged_at"), datetime):
                item["logged_at"] = item["logged_at"].strftime("%H:%M")
            meals.append(item)

            total_cals += float(item.get("calories") or 0)
            total_prot += float(item.get("protein_g") or 0)
            total_carbs += float(item.get("carbs_g") or 0)
            total_fat += float(item.get("fat_g") or 0)

    # Get target calories from user profile
    profile = get_user_profile(user_id) or {}
    target_cals = profile.get("target_calories") or 2000
    target_protein = profile.get("target_protein_g") or 75
    target_carbs = profile.get("target_carbs_g") or 250
    target_fat = profile.get("target_fat_g") or 55

    return {
        "date": str(target_date),
        "total_calories": round(total_cals, 1),
        "total_protein_g": round(total_prot, 1),
        "total_carbs_g": round(total_carbs, 1),
        "total_fat_g": round(total_fat, 1),
        "target_calories": target_cals,
        "target_protein_g": target_protein,
        "target_carbs_g": target_carbs,
        "target_fat_g": target_fat,
        "calories_remaining": max(0, round(target_cals - total_cals, 1)),
        "percent_target": min(100, round((total_cals / target_cals) * 100, 1)) if target_cals else 0,
        "meals": meals,
    }


def delete_meal_log(log_id, user_id):
    """Delete a logged meal entry."""
    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            cursor.execute("DELETE FROM calorie_logs WHERE id = %s AND user_id = %s;", (log_id, user_id))
        else:
            cursor.execute("DELETE FROM calorie_logs WHERE id = ? AND user_id = ?;", (log_id, user_id))
        deleted = cursor.rowcount > 0
        cursor.close()
    return deleted


# ─────────────────────────────────────────────────────────────
# User Account / Authenticator Operations
# ─────────────────────────────────────────────────────────────

def register_account(user_id, password, name="", role="user"):
    """Register a new user account with hashed password."""
    try:
        user_id = str(user_id or "").strip().lower().replace(" ", "_")
        if not user_id or not password:
            return None, "User ID and password are required."
        if len(user_id) < 2:
            return None, "User ID must be at least 2 characters long."
        if len(password) < 4:
            return None, "Password must be at least 4 characters long."

        pw_hash = generate_password_hash(password)
        name = str(name or "").strip() or user_id

        def _do_register_insert():
            with DatabaseConnection() as (conn, is_pg):
                cursor = conn.cursor()
                if is_pg:
                    cursor.execute("SELECT user_id FROM users_auth WHERE user_id = %s;", (user_id,))
                else:
                    cursor.execute("SELECT user_id FROM users_auth WHERE user_id = ?;", (user_id,))
                if cursor.fetchone():
                    cursor.close()
                    return None, "User ID already exists. Please choose a different ID or Sign In."

                if is_pg:
                    cursor.execute("""
                        INSERT INTO users_auth (user_id, password_hash, name, role)
                        VALUES (%s, %s, %s, %s);
                    """, (user_id, pw_hash, name, role))
                else:
                    cursor.execute("""
                        INSERT INTO users_auth (user_id, password_hash, name, role)
                        VALUES (?, ?, ?, ?);
                    """, (user_id, pw_hash, name, role))
                cursor.close()
            return {"user_id": user_id, "name": name, "role": role}, None

        try:
            user_data, err = _do_register_insert()
            if err:
                return None, err
        except Exception as insert_err:
            err_str = str(insert_err).lower()
            if "users_auth" in err_str and ("does not exist" in err_str or "no such table" in err_str):
                logger.warning("users_auth table missing during register_account. Initializing database and retrying...")
                init_db()
                user_data, err = _do_register_insert()
                if err:
                    return None, err
            else:
                logger.error(f"Error during registration insert for {user_id}: {insert_err}", exc_info=True)
                return None, f"Database error during registration: {str(insert_err)}"

        # Attempt to create initial user profile record
        try:
            save_user_profile({"user_id": user_id, "name": name})
        except Exception as prof_err:
            logger.warning(f"Failed to create default user profile for {user_id}: {prof_err}")

        return user_data, None
    except Exception as e:
        logger.error(f"Unexpected error in register_account for {user_id}: {e}", exc_info=True)
        return None, f"Could not create account: {str(e)}"


def authenticate_account(user_id, password):
    """Authenticate user with user_id and password."""
    try:
        user_id = str(user_id or "").strip().lower().replace(" ", "_")
        if not user_id or not password:
            return None, "User ID and password are required."

        def _do_auth_lookup():
            with DatabaseConnection() as (conn, is_pg):
                if is_pg:
                    from psycopg2.extras import RealDictCursor
                    cursor = conn.cursor(cursor_factory=RealDictCursor)
                    cursor.execute("SELECT user_id, password_hash, name, role FROM users_auth WHERE user_id = %s;", (user_id,))
                    row = cursor.fetchone()
                    cursor.close()
                else:
                    cursor = conn.cursor()
                    cursor.execute("SELECT user_id, password_hash, name, role FROM users_auth WHERE user_id = ?;", (user_id,))
                    row = cursor.fetchone()
                    cursor.close()
                return row

        try:
            row = _do_auth_lookup()
        except Exception as lookup_err:
            err_str = str(lookup_err).lower()
            if "users_auth" in err_str and ("does not exist" in err_str or "no such table" in err_str):
                logger.warning("users_auth table missing during authenticate_account. Initializing database and retrying...")
                init_db()
                row = _do_auth_lookup()
            else:
                logger.error(f"Error during auth lookup for {user_id}: {lookup_err}", exc_info=True)
                return None, f"Database error during authentication: {str(lookup_err)}"

        if not row:
            return None, "User ID not found. Please register first."

        record = dict(row)
        if not check_password_hash(record["password_hash"], password):
            return None, "Incorrect password. Please try again."

        return {
            "user_id": record["user_id"],
            "name": record.get("name") or record["user_id"],
            "role": record.get("role") or "user",
        }, None
    except Exception as e:
        logger.error(f"Unexpected error in authenticate_account for {user_id}: {e}", exc_info=True)
        return None, f"Authentication error: {str(e)}"


def get_account(user_id):
    """Get account info without password hash."""
    try:
        user_id = str(user_id or "").strip().lower().replace(" ", "_")
        if not user_id:
            return None

        def _do_get():
            with DatabaseConnection() as (conn, is_pg):
                if is_pg:
                    from psycopg2.extras import RealDictCursor
                    cursor = conn.cursor(cursor_factory=RealDictCursor)
                    cursor.execute("SELECT user_id, name, role, created_at FROM users_auth WHERE user_id = %s;", (user_id,))
                    row = cursor.fetchone()
                    cursor.close()
                else:
                    cursor = conn.cursor()
                    cursor.execute("SELECT user_id, name, role, created_at FROM users_auth WHERE user_id = ?;", (user_id,))
                    row = cursor.fetchone()
                    cursor.close()
                if row:
                    return dict(row)
                return None

        try:
            return _do_get()
        except Exception as e:
            err_str = str(e).lower()
            if "users_auth" in err_str and ("does not exist" in err_str or "no such table" in err_str):
                init_db()
                return _do_get()
            logger.warning(f"Error getting account info for {user_id}: {e}")
            return None
    except Exception as ex:
        logger.warning(f"Unexpected error in get_account: {ex}")
        return None


# ─────────────────────────────────────────────────────────────
# Feedback Operations (Private & Developer Views)
# ─────────────────────────────────────────────────────────────

def save_feedback_db(fb_id, user_id, name, rating, message):
    """Save user feedback in database."""
    user_id = str(user_id or "anonymous").strip().lower()
    name = str(name).strip() or "User"
    message = str(message).strip()
    try:
        rating = max(1, min(5, int(rating)))
    except (ValueError, TypeError):
        rating = 5

    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            cursor.execute("""
                INSERT INTO feedbacks (id, user_id, name, rating, message, created_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP);
            """, (fb_id, user_id, name, rating, message))
        else:
            cursor.execute("""
                INSERT INTO feedbacks (id, user_id, name, rating, message, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """, (fb_id, user_id, name, rating, message))
        cursor.close()

    return {
        "id": fb_id,
        "user_id": user_id,
        "name": name,
        "rating": rating,
        "message": message,
        "date": "Just now",
    }


def get_user_feedbacks(user_id):
    """Fetch ONLY feedback submitted by this specific user."""
    user_id = str(user_id).strip().lower()
    results = []
    with DatabaseConnection() as (conn, is_pg):
        if is_pg:
            from psycopg2.extras import RealDictCursor
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT id, user_id, name, rating, message, created_at
                FROM feedbacks WHERE user_id = %s
                ORDER BY created_at DESC;
            """, (user_id,))
            rows = cursor.fetchall()
            cursor.close()
        else:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, name, rating, message, created_at
                FROM feedbacks WHERE user_id = ?
                ORDER BY created_at DESC;
            """, (user_id,))
            rows = cursor.fetchall()
            cursor.close()

        for r in rows:
            item = dict(r)
            if isinstance(item.get("created_at"), datetime):
                item["date"] = item["created_at"].strftime("%b %d, %I:%M %p")
            else:
                item["date"] = "Recently"
            results.append(item)
    return results


def get_developer_feedbacks():
    """Fetch ALL feedbacks across all users (Developer view only)."""
    results = []
    with DatabaseConnection() as (conn, is_pg):
        if is_pg:
            from psycopg2.extras import RealDictCursor
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT id, user_id, name, rating, message, created_at
                FROM feedbacks
                ORDER BY created_at DESC;
            """, ())
            rows = cursor.fetchall()
            cursor.close()
        else:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, name, rating, message, created_at
                FROM feedbacks
                ORDER BY created_at DESC;
            """)
            rows = cursor.fetchall()
            cursor.close()

        for r in rows:
            item = dict(r)
            if isinstance(item.get("created_at"), datetime):
                item["date"] = item["created_at"].strftime("%b %d, %I:%M %p")
            else:
                item["date"] = "Recently"
            results.append(item)
    return results


def get_all_feedbacks():
    """Fetch ALL feedbacks across all users."""
    return get_developer_feedbacks()


def update_feedback_db(fb_id, user_id, rating, message, is_dev=False):
    """Update an existing feedback entry if owned by user_id or if developer."""
    fb_id = str(fb_id).strip()
    user_id = str(user_id).strip().lower()
    message = str(message).strip()
    if not message:
        return None, "Feedback message cannot be empty."
    try:
        rating = max(1, min(5, int(rating)))
    except (ValueError, TypeError):
        rating = 5

    with DatabaseConnection() as (conn, is_pg):
        cursor = conn.cursor()
        if is_pg:
            if is_dev:
                cursor.execute("""
                    UPDATE feedbacks
                    SET rating = %s, message = %s
                    WHERE id = %s
                    RETURNING id, user_id, name, rating, message, created_at;
                """, (rating, message, fb_id))
            else:
                cursor.execute("""
                    UPDATE feedbacks
                    SET rating = %s, message = %s
                    WHERE id = %s AND user_id = %s
                    RETURNING id, user_id, name, rating, message, created_at;
                """, (rating, message, fb_id, user_id))
            row = cursor.fetchone()
        else:
            if is_dev:
                cursor.execute("""
                    SELECT id, user_id, name, rating, message, created_at
                    FROM feedbacks WHERE id = ?;
                """, (fb_id,))
            else:
                cursor.execute("""
                    SELECT id, user_id, name, rating, message, created_at
                    FROM feedbacks WHERE id = ? AND user_id = ?;
                """, (fb_id, user_id))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("""
                    UPDATE feedbacks
                    SET rating = ?, message = ?
                    WHERE id = ?;
                """, (rating, message, fb_id))
                row = (existing[0], existing[1], existing[2], rating, message, existing[5])
            else:
                row = None
        cursor.close()

    if not row:
        return None, "Feedback entry not found or permission denied."

    item = {
        "id": row[0],
        "user_id": row[1],
        "name": row[2],
        "rating": row[3],
        "message": row[4],
        "date": "Edited recently"
    }
    return item, None


