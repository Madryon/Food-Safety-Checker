from functools import wraps
from datetime import datetime
from flask import Blueprint, abort, session, redirect, url_for, flash
from flask_login import current_user, login_user

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    """Decorator to protect admin routes.
    Unauthorized users are redirected to the Admin Sign In page with a clear notification.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Fallback: if current_user is not loaded yet, rehydrate from session
        if not current_user.is_authenticated:
            sess_uid = session.get("user_id")
            if sess_uid:
                try:
                    from models import User
                    u = User.query.filter_by(username=str(sess_uid).strip().lower()).first()
                    is_u_adm = getattr(u, "is_admin", False) or getattr(u, "role", "") in ("admin", "developer")
                    if u and is_u_adm and getattr(u, "is_active", True) and not getattr(u, "is_locked", False):
                        login_user(u)
                except Exception:
                    pass

        # 2. Check authentication and admin privileges (supports both ORM current_user & session role)
        sess_role = str(session.get("role") or "").strip().lower()
        is_sess_admin = sess_role in ("admin", "developer")
        is_user_admin = current_user.is_authenticated and (
            getattr(current_user, "is_admin", False) or getattr(current_user, "role", "") in ("admin", "developer")
        )

        if not current_user.is_authenticated and not is_sess_admin:
            flash("Please sign in as Administrator to access the Admin Console.", "warning")
            return redirect(url_for("admin.admin_login"))

        if not (is_user_admin or is_sess_admin):
            flash("Administrator privileges required. Please sign in with an Admin account.", "danger")
            return redirect(url_for("admin.admin_login"))

        # Refresh admin session activity timestamp
        try:
            session["admin_last_active"] = datetime.utcnow().timestamp()
        except Exception:
            pass

        return f(*args, **kwargs)
    return decorated_function


from . import routes
