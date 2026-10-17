from functools import wraps
from datetime import datetime
from flask import Blueprint, abort, session, redirect, url_for, flash
from flask_login import current_user

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    """Decorator to protect admin routes.
    CRITICAL SECURITY RULE: Unauthorized users (anonymous or standard users)
    receive an immediate 404 NOT FOUND so the admin panel's existence is hidden.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            # Obscure admin panel existence
            abort(404)

        # Check admin session inactivity timeout (30 minutes)
        last_activity = session.get("admin_last_active")
        now = datetime.utcnow().timestamp()
        if last_activity:
            if now - last_activity > 1800:  # 30 minutes
                session.pop("admin_last_active", None)
                flash("Admin session expired due to 30 minutes of inactivity. Please log in again.", "warning")
                return redirect(url_for("admin.admin_login"))
        session["admin_last_active"] = now

        return f(*args, **kwargs)
    return decorated_function


from . import routes
