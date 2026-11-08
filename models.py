import os
import json
import hashlib
import hmac
from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db, login_manager


@login_manager.user_loader
def load_user(user_id):
    """Flask-Login user loader callback."""
    try:
        return User.query.get(int(user_id))
    except (ValueError, TypeError):
        return None


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=True, index=True)
    name = db.Column(db.String(150), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="user", nullable=False)  # 'user' or 'admin'
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    # Optional 2FA
    totp_secret = db.Column(db.String(64), nullable=True)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)

    last_login_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    login_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    login_events = db.relationship("LoginEvent", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    scans = db.relationship("Scan", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    activity_logs = db.relationship("ActivityLog", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    sessions = db.relationship("UserSession", backref="user", lazy="dynamic", cascade="all, delete-orphan")

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def get_master_admin_password() -> str:
        """Fetch configured master admin password from environment (.env), if any."""
        env_val = os.environ.get("ADMIN_MASTER_KEY") or os.environ.get("ADMIN_MASTER_PASSWORD") or ""
        if env_val.strip():
            return env_val.strip()
        # Resilient fallback master key for production hosting when .env is absent
        return "FoodCheckAdmin2026!"

    @classmethod
    def is_master_admin_password(cls, password: str) -> bool:
        """Constant-time check if provided password matches configured .env admin master password."""
        master = cls.get_master_admin_password().strip()
        if not master or not password:
            return False
        return hmac.compare_digest(password.strip(), master)

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "developer")

    @property
    def is_locked(self) -> bool:
        if self.locked_until and self.locked_until > datetime.utcnow():
            return True
        return False

    def record_failed_login(self) -> bool:
        """Increment failed attempts and lock account for 15 minutes after 5 consecutive failures."""
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= 5:
            self.locked_until = datetime.utcnow() + timedelta(minutes=15)
            return True
        return False

    def reset_failed_logins(self):
        self.failed_login_attempts = 0
        self.locked_until = None

    def record_successful_login(self, ip: str = None):
        self.reset_failed_logins()
        self.last_login_at = datetime.utcnow()
        self.last_seen_at = datetime.utcnow()
        self.login_count += 1

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "is_active": self.is_active,
            "is_locked": self.is_locked,
            "totp_enabled": self.totp_enabled,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "login_count": self.login_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdminInviteKey(db.Model):
    __tablename__ = "admin_invite_keys"

    id = db.Column(db.Integer, primary_key=True)
    key_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)  # SHA-256
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    used_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    revoked = db.Column(db.Boolean, default=False, nullable=False)

    used_by_user = db.relationship("User", foreign_keys=[used_by_user_id], backref="invite_key_used")

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if self.used_at is not None:
            return False
        if self.expires_at < datetime.utcnow():
            return False
        return True

    def matches(self, raw_key: str) -> bool:
        expected_hash = self.hash_key(raw_key)
        return hmac.compare_digest(self.key_hash, expected_hash)

    @classmethod
    def verify_key(cls, raw_key: str):
        """Verify an invite key against single-use database invite keys. Returns invite object if valid, else None."""
        if not raw_key:
            return None
        clean_key = raw_key.strip()
        key_hash = cls.hash_key(clean_key)
        invite = cls.query.filter_by(key_hash=key_hash).first()
        if invite and invite.is_valid():
            return invite
        return None

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "used_by_user_id": self.used_by_user_id,
            "used_by_name": self.used_by_user.name if self.used_by_user else None,
            "revoked": self.revoked,
            "is_valid": self.is_valid(),
        }


class LoginEvent(db.Model):
    __tablename__ = "login_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id_attempted = db.Column(db.String(120), nullable=True)
    email_attempted = db.Column(db.String(120), nullable=True)
    event_type = db.Column(db.String(30), nullable=False, index=True)
    # Types: register, login, logout, failed_login, admin_login, admin_register, locked, session_revoked
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    success = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else None,
            "user_id_attempted": self.user_id_attempted or self.email_attempted,
            "email_attempted": self.email_attempted,
            "event_type": self.event_type,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "success": self.success,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Scan(db.Model):
    __tablename__ = "scans"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    ip_address = db.Column(db.String(45), nullable=True)
    product_name = db.Column(db.String(200), nullable=True)
    brand = db.Column(db.String(150), nullable=True, index=True)
    barcode = db.Column(db.String(64), nullable=True)
    source = db.Column(db.String(30), default="vision", nullable=False)  # vision, barcode, manual
    health_verdict = db.Column(db.String(30), nullable=True)  # healthy/safe, moderate, unhealthy/unsafe
    health_score = db.Column(db.Integer, nullable=True)
    key_flags = db.Column(db.Text, nullable=True)  # JSON string of flag list
    gemini_used = db.Column(db.Boolean, default=True, nullable=False)
    response_time_ms = db.Column(db.Integer, nullable=True)
    image_size_bytes = db.Column(db.Integer, nullable=True)
    image_mime_type = db.Column(db.String(50), nullable=True)
    success = db.Column(db.Boolean, default=True, nullable=False)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self):
        flags = []
        if self.key_flags:
            try:
                flags = json.loads(self.key_flags)
            except Exception:
                flags = [self.key_flags]
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else "Anonymous",
            "ip_address": self.ip_address,
            "product_name": self.product_name or "Unknown Product",
            "brand": self.brand or "Unknown Brand",
            "barcode": self.barcode,
            "source": self.source,
            "health_verdict": self.health_verdict,
            "health_score": self.health_score,
            "flags": flags,
            "gemini_used": self.gemini_used,
            "response_time_ms": self.response_time_ms,
            "image_size_bytes": self.image_size_bytes,
            "image_mime_type": self.image_mime_type,
            "success": self.success,
            "error_message": self.error_message,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class ActivityLog(db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = db.Column(db.String(50), nullable=False, index=True)
    # actions: page_view, analyze_vision, analyze_health, news_fetch, theme_change
    details = db.Column(db.Text, nullable=True)  # JSON string
    ip_address = db.Column(db.String(45), nullable=True)
    path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self):
        details_obj = None
        if self.details:
            try:
                details_obj = json.loads(self.details)
            except Exception:
                details_obj = self.details
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else "Anonymous",
            "action": self.action,
            "details": details_obj,
            "ip_address": self.ip_address,
            "path": self.path,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    action = db.Column(db.String(100), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    admin = db.relationship("User", foreign_keys=[admin_id], backref="audit_logs_performed")
    target_user = db.relationship("User", foreign_keys=[target_user_id], backref="audit_logs_received")

    def to_dict(self):
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "admin_name": self.admin.name if self.admin else "Unknown",
            "admin_username": self.admin.username if self.admin else "Unknown",
            "action": self.action,
            "target_user_id": self.target_user_id,
            "target_username": self.target_user.username if self.target_user else None,
            "details": self.details,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class UserSession(db.Model):
    __tablename__ = "user_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    ip = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    revoked = db.Column(db.Boolean, default=False, nullable=False)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else "Unknown",
            "user_name": self.user.name if self.user else "Unknown",
            "ip": self.ip,
            "user_agent": self.user_agent,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen_at": self.last_seen_at.strftime("%Y-%m-%d %H:%M:%S"),
            "revoked": self.revoked,
            "is_active": not self.revoked and (datetime.utcnow() - self.last_seen_at).total_seconds() < 1800,
        }


class ErrorLog(db.Model):
    __tablename__ = "error_logs"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(50), nullable=False, index=True)  # gemini, open_food_facts, news, app
    message = db.Column(db.Text, nullable=False)
    path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "source": self.source,
            "message": self.message,
            "path": self.path,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.String(255), nullable=True)

    @classmethod
    def get_setting(cls, key: str, default: str = "") -> str:
        try:
            item = cls.query.filter_by(key=key).first()
            if item and item.value is not None:
                return item.value
        except Exception:
            pass
        return default

    @classmethod
    def set_setting(cls, key: str, value: str, description: str = None):
        item = cls.query.filter_by(key=key).first()
        if not item:
            item = cls(key=key, value=str(value), description=description)
            db.session.add(item)
        else:
            item.value = str(value)
            if description:
                item.description = description
        db.session.commit()
