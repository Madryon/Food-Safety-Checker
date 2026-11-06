import json
import logging
from datetime import datetime, date
from flask import request, has_request_context
from flask_login import current_user
from extensions import db
from models import (
    ActivityLog,
    LoginEvent,
    Scan,
    ErrorLog,
    AdminAuditLog,
    UserSession,
    AppSetting,
    User,
)

logger = logging.getLogger(__name__)


def get_client_ip() -> str:
    """Extract real client IP considering reverse proxy headers."""
    if not has_request_context():
        return "127.0.0.1"
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def get_current_user_id():
    """Retrieve currently authenticated user's ID if in request context."""
    if has_request_context() and current_user and getattr(current_user, "is_authenticated", False):
        return current_user.id
    return None


def log_activity(action: str, details=None, user_id=None, ip: str = None, path: str = None):
    """Safely log general application activity. NEVER raises an exception."""
    try:
        uid = user_id or get_current_user_id()
        client_ip = ip or get_client_ip()
        request_path = path or (request.path if has_request_context() else None)

        details_str = None
        if details is not None:
            if isinstance(details, (dict, list)):
                details_str = json.dumps(details)
            else:
                details_str = str(details)

        entry = ActivityLog(
            user_id=uid,
            action=action,
            details=details_str,
            ip_address=client_ip,
            path=request_path,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to record activity log: {e}")


def log_login_event(
    event_type: str,
    user_id=None,
    email_attempted: str = None,
    user_id_attempted: str = None,
    success: bool = True,
    ip: str = None,
    user_agent: str = None,
):
    """Log an authentication event (register, login, failed_login, locked, etc.)."""
    try:
        attempted = user_id_attempted or email_attempted
        client_ip = ip or get_client_ip()
        ua = user_agent or (request.headers.get("User-Agent") if has_request_context() else "")
        if ua and len(ua) > 250:
            ua = ua[:250]

        event = LoginEvent(
            user_id=user_id,
            user_id_attempted=attempted,
            email_attempted=attempted,
            event_type=event_type,
            ip_address=client_ip,
            user_agent=ua,
            success=success,
        )
        db.session.add(event)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to record login event: {e}")


def log_scan(
    product_name: str = None,
    brand: str = None,
    barcode: str = None,
    source: str = "vision",
    health_verdict: str = None,
    health_score: int = None,
    key_flags=None,
    gemini_used: bool = True,
    response_time_ms: int = None,
    image_size_bytes: int = None,
    image_mime_type: str = None,
    success: bool = True,
    error_message: str = None,
    user_id=None,
    ip: str = None,
) -> int:
    """Log product safety/health scan metadata. NEVER stores raw image contents."""
    try:
        uid = user_id or get_current_user_id()
        client_ip = ip or get_client_ip()

        flags_str = None
        if key_flags is not None:
            if isinstance(key_flags, (dict, list)):
                flags_str = json.dumps(key_flags)
            else:
                flags_str = str(key_flags)

        scan = Scan(
            user_id=uid,
            ip_address=client_ip,
            product_name=(product_name[:195] if product_name else None),
            brand=(brand[:145] if brand else None),
            barcode=(barcode[:60] if barcode else None),
            source=source,
            health_verdict=health_verdict,
            health_score=health_score,
            key_flags=flags_str,
            gemini_used=gemini_used,
            response_time_ms=response_time_ms,
            image_size_bytes=image_size_bytes,
            image_mime_type=image_mime_type,
            success=success,
            error_message=error_message,
        )
        db.session.add(scan)
        db.session.commit()
        return scan.id
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to record scan log: {e}")
        return None


def log_error(source: str, message: str, path: str = None):
    """Log external API errors or system failures into error_logs."""
    try:
        request_path = path or (request.path if has_request_context() else None)
        entry = ErrorLog(
            source=source,
            message=str(message),
            path=request_path,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to record error log: {e}")


def log_admin_audit(admin_id: int, action: str, target_user_id: int = None, details=None):
    """Log an administrative action into the audit trail."""
    try:
        details_str = None
        if details is not None:
            if isinstance(details, (dict, list)):
                details_str = json.dumps(details)
            else:
                details_str = str(details)

        entry = AdminAuditLog(
            admin_id=admin_id,
            action=action,
            target_user_id=target_user_id,
            details=details_str,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to record admin audit log: {e}")


def check_daily_scan_limit(user_id=None, ip: str = None) -> tuple[bool, int, int]:
    """Check whether user/anonymous IP has reached their daily scan quota.
    Returns: (is_allowed: bool, remaining_scans: int, limit: int)
    """
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        uid = user_id or get_current_user_id()
        client_ip = ip or get_client_ip()

        if uid:
            limit_str = AppSetting.get_setting("daily_scan_limit_user", "100")
            limit = int(limit_str) if limit_str.isdigit() else 100
            count = Scan.query.filter(
                Scan.user_id == uid,
                Scan.created_at >= today_start
            ).count()
        else:
            limit_str = AppSetting.get_setting("daily_scan_limit_anon", "25")
            limit = int(limit_str) if limit_str.isdigit() else 25
            count = Scan.query.filter(
                Scan.user_id.is_(None),
                Scan.ip_address == client_ip,
                Scan.created_at >= today_start
            ).count()

        remaining = max(0, limit - count)
        return (count < limit), remaining, limit
    except Exception as e:
        logger.warning(f"Error checking daily scan limit: {e}")
        return True, 999, 999


def is_suspicious_login(user_id: int, current_ip: str, current_ua: str) -> bool:
    """Determine if a login is from a new/unseen IP address or user agent for this user."""
    try:
        # Check previous successful logins
        past_events = LoginEvent.query.filter(
            LoginEvent.user_id == user_id,
            LoginEvent.success == True
        ).all()
        if not past_events:
            return False  # First login isn't suspicious

        known_ips = {ev.ip_address for ev in past_events if ev.ip_address}
        return current_ip not in known_ips
    except Exception:
        return False
