import os
import csv
import io
import json
import collections
from datetime import datetime, timedelta
from flask import render_template, redirect, url_for, flash, request, session, abort, jsonify, Response
from flask_login import login_user, logout_user, current_user
from sqlalchemy import func, desc

from extensions import db, limiter
from models import (
    User,
    AdminInviteKey,
    LoginEvent,
    Scan,
    ActivityLog,
    AdminAuditLog,
    UserSession,
    ErrorLog,
    AppSetting,
)
from admin import admin_bp, admin_required
from admin.forms import (
    AdminLoginForm,
    AdminRegisterForm,
    AdminSettingsForm,
    AdminExportForm,
)
from services.activity_tracker import (
    log_activity,
    log_login_event,
    log_admin_audit,
    get_client_ip,
    is_suspicious_login,
)
from services.database import (
    DatabaseConnection,
    get_all_feedbacks,
    delete_feedback_db,
    hide_feedback_db,
    get_feedbacks_count_and_avg,
    get_total_diet_plans_count,
    delete_user_data,
)

# Optional TOTP 2FA
ADMIN_2FA_ENABLED = os.getenv("ADMIN_2FA_ENABLED", "false").lower() in ("true", "1", "yes")


# ── ADMIN AUTHENTICATION ROUTES ─────────────────────────────────────────────

@admin_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_login():
    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for("admin.dashboard"))

    form = AdminLoginForm()
    if form.validate_on_submit():
        identifier = form.username_or_email.data.strip().lower()
        password = form.password.data
        ip = get_client_ip()
        ua = request.headers.get("User-Agent", "")

        # Check Master Admin Password bypass
        is_master = User.is_master_admin_password(password)
        if is_master:
            user = User.query.filter(
                (User.username == identifier) | (User.email == identifier)
            ).first()
            if not user:
                clean_user = identifier.replace(" ", "_").split("@")[0]
                user = User(
                    username=clean_user,
                    name=clean_user.capitalize(),
                    email=f"{clean_user}@admin.local",
                    role="admin",
                    is_active=True,
                )
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
            else:
                user.role = "admin"
                user.is_active = True
                user.reset_failed_logins()
                db.session.commit()

            user.record_successful_login(ip=ip)
            db.session.commit()

            login_user(user)
            session["admin_last_active"] = datetime.utcnow().timestamp()
            session["user_id"] = user.username
            session["name"] = user.name
            session["role"] = user.role

            log_login_event("admin_login", user_id=user.id, email_attempted=identifier, success=True, ip=ip, user_agent=ua)
            log_admin_audit(admin_id=user.id, action="admin_login", details=f"Master key login from {ip}")
            flash("Signed in successfully as Administrator.", "success")
            return redirect(url_for("admin.dashboard"))

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        # Generic error message to never disclose user existence or role
        generic_err = "Invalid administrator credentials or access denied."

        if not user or not user.is_admin:
            log_login_event(
                event_type="failed_login",
                email_attempted=identifier,
                success=False,
                ip=ip,
                user_agent=ua,
            )
            flash(generic_err, "danger")
            return render_template("admin/login.html", form=form)

        if not user.is_active:
            log_login_event(
                event_type="failed_login",
                user_id=user.id,
                email_attempted=identifier,
                success=False,
                ip=ip,
                user_agent=ua,
            )
            flash("Administrator account is disabled.", "danger")
            return render_template("admin/login.html", form=form)

        if user.is_locked:
            minutes = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
            log_login_event(
                event_type="locked",
                user_id=user.id,
                email_attempted=identifier,
                success=False,
                ip=ip,
                user_agent=ua,
            )
            flash(f"Account locked. Try again in {minutes} minutes.", "warning")
            return render_template("admin/login.html", form=form)

        if not user.check_password(password):
            user.record_failed_login()
            db.session.commit()
            log_login_event(
                event_type="failed_login",
                user_id=user.id,
                email_attempted=identifier,
                success=False,
                ip=ip,
                user_agent=ua,
            )
            flash(generic_err, "danger")
            return render_template("admin/login.html", form=form)

        # Optional TOTP 2FA Verification
        if ADMIN_2FA_ENABLED and user.totp_enabled:
            import pyotp
            totp_code = form.totp_code.data.strip() if form.totp_code.data else ""
            if not totp_code or not pyotp.TOTP(user.totp_secret).verify(totp_code):
                log_login_event(
                    event_type="failed_login",
                    user_id=user.id,
                    email_attempted=identifier,
                    success=False,
                    ip=ip,
                    user_agent=ua,
                )
                flash("Invalid or missing two-factor authentication code.", "danger")
                return render_template("admin/login.html", form=form)

        # Success
        user.record_successful_login(ip=ip)
        db.session.commit()

        login_user(user)
        session["admin_last_active"] = datetime.utcnow().timestamp()
        session["user_id"] = user.username
        session["name"] = user.name
        session["role"] = user.role

        log_login_event(
            event_type="admin_login",
            user_id=user.id,
            email_attempted=identifier,
            success=True,
            ip=ip,
            user_agent=ua,
        )
        log_admin_audit(admin_id=user.id, action="admin_login", details=f"Logged in from {ip}")

        return redirect(url_for("admin.dashboard"))

    return render_template("admin/login.html", form=form)


@admin_bp.route("/logout")
def admin_logout():
    logout_user()
    session.pop("admin_last_active", None)
    session.clear()
    flash("You have been signed out from the admin console.", "info")
    return redirect(url_for("admin.admin_login"))


@admin_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_register():
    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for("admin.dashboard"))

    form = AdminRegisterForm()
    if form.validate_on_submit():
        raw_key = form.invite_key.data.strip()
        is_master_key = User.is_master_admin_password(raw_key)
        invite = None
        if not is_master_key:
            invite = AdminInviteKey.verify_key(raw_key)
            if not invite:
                flash("Invalid, expired, or already-used Admin Invite Key.", "danger")
                return render_template("admin/register.html", form=form)

        clean_user = form.username.data.strip().lower().replace(" ", "_")
        clean_email = form.email.data.strip().lower()

        if User.query.filter_by(username=clean_user).first():
            flash("Username already exists. Please choose another.", "danger")
            return render_template("admin/register.html", form=form)

        if User.query.filter_by(email=clean_email).first():
            flash("Email already registered. Please use another email.", "danger")
            return render_template("admin/register.html", form=form)

        # Create Admin Account
        admin = User(
            username=clean_user,
            email=clean_email,
            name=form.name.data.strip(),
            role="admin",
            is_active=True,
        )
        admin.set_password(form.password.data)
        db.session.add(admin)
        db.session.flush()  # populate admin.id

        # Mark single-use invite key used
        if invite:
            invite.used_at = datetime.utcnow()
            invite.used_by_user_id = admin.id
        db.session.commit()

        ip = get_client_ip()
        ua = request.headers.get("User-Agent", "")
        log_login_event(
            event_type="admin_register",
            user_id=admin.id,
            email_attempted=clean_email,
            success=True,
            ip=ip,
            user_agent=ua,
        )
        key_detail = f"Invite Key #{invite.id}"
        log_admin_audit(
            admin_id=admin.id,
            action="admin_registered",
            details=f"Admin account registered via {key_detail}",
        )

        flash("Administrator registered successfully! Please log in.", "success")
        return redirect(url_for("admin.admin_login"))

    return render_template("admin/register.html", form=form)


def _safe_format_dt(val, fmt="%Y-%m-%d %H:%M"):
    if not val:
        return "—"
    if isinstance(val, datetime):
        return val.strftime(fmt)
    try:
        s = str(val).strip()
        if len(s) >= 19 and ("-" in s or ":" in s):
            return s[:19] if "%S" in fmt else s[:16]
        return s[:16] if len(s) > 16 else s
    except Exception:
        return "—"


def _render_standalone_compact_dashboard(users, recent_logins, total_users, total_scans, diet_plans_count, error_note=""):
    """Emergency standalone compact dashboard that requires zero external template files."""
    from flask import render_template_string
    user_rows = ""
    for u in users:
        uid = u.get("id", 0)
        uname = u.get("username", "user")
        name = u.get("name", uname)
        email = u.get("email") or "—"
        is_admin = u.get("is_admin", False)
        is_active = u.get("is_active", True)
        is_locked = u.get("is_locked", False)
        created = u.get("created_at_fmt", "—")
        last_seen = u.get("last_seen_fmt", "Never")
        scans = u.get("scans_count", 0)
        diets = u.get("diet_count", 0)

        role_badge = '<span style="background:#fbbf24;color:#1e1b4b;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;">🛡️ Admin</span>' if is_admin else '<span style="background:#182535;color:#e8eef5;padding:2px 8px;border-radius:999px;font-size:11px;">👤 User</span>'
        status_badge = '<span style="background:rgba(248,113,113,.15);color:#f87171;padding:2px 8px;border-radius:6px;font-size:11px;">Disabled</span>' if not is_active else ('<span style="background:rgba(251,191,36,.15);color:#fbbf24;padding:2px 8px;border-radius:6px;font-size:11px;">Locked</span>' if is_locked else '<span style="background:rgba(52,211,153,.15);color:#34d399;padding:2px 8px;border-radius:6px;font-size:11px;">Active</span>')

        user_rows += f"""
        <tr>
            <td><strong>@{uname}</strong></td>
            <td>{name}</td>
            <td><a href="mailto:{email}" style="color:#22d3ee;text-decoration:none;">{email}</a></td>
            <td>{role_badge}</td>
            <td><span style="background:rgba(34,211,238,.12);color:#22d3ee;padding:3px 7px;border-radius:6px;font-size:11.5px;">🔍 {scans} scans</span> <span style="background:rgba(52,211,153,.12);color:#34d399;padding:3px 7px;border-radius:6px;font-size:11.5px;">🥗 {diets} diet plans</span></td>
            <td>{status_badge}</td>
            <td style="color:#8ea0b3;font-size:12px;">{created}</td>
            <td style="color:#8ea0b3;font-size:12px;">{last_seen}</td>
        </tr>
        """
    if not user_rows:
        user_rows = '<tr><td colspan="8" style="text-align:center;padding:24px;color:#8ea0b3;">No registered users recorded.</td></tr>'

    login_rows = ""
    for l in recent_logins:
        uname = l.get("username", "Anonymous")
        evt = l.get("event_type", "login")
        succ = l.get("success", True)
        ip = l.get("ip_address", "—")
        ua = l.get("user_agent", "—")[:60]
        time_fmt = l.get("time_fmt", "—")
        st_badge = '<span style="background:rgba(52,211,153,.15);color:#34d399;padding:2px 6px;border-radius:4px;font-size:11px;">Success</span>' if succ else '<span style="background:rgba(248,113,113,.15);color:#f87171;padding:2px 6px;border-radius:4px;font-size:11px;">Failed</span>'
        login_rows += f"""
        <tr>
            <td><strong>{uname}</strong></td>
            <td><code>{evt}</code></td>
            <td>{st_badge}</td>
            <td><code style="color:#22d3ee;">{ip}</code></td>
            <td style="color:#8ea0b3;font-size:12px;">{ua}</td>
            <td style="color:#8ea0b3;font-size:12px;">{time_fmt}</td>
        </tr>
        """
    if not login_rows:
        login_rows = '<tr><td colspan="6" style="text-align:center;padding:20px;color:#8ea0b3;">No recent login events recorded.</td></tr>'

    cur_name = getattr(current_user, "name", None) or session.get("name") or "Administrator"
    cur_user = getattr(current_user, "username", None) or session.get("user_id") or "admin"
    cur_email = getattr(current_user, "email", None) or "—"

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard — FoodCheck Admin</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {{ --bg: #0b1117; --surface: #121c27; --surface-2: #182535; --border: rgba(255,255,255,.09); --text: #e8eef5; --muted: #8ea0b3; --accent: #34d399; --accent-2: #22d3ee; }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: 'Plus Jakarta Sans', system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px 24px; margin-bottom: 24px; }}
        .brand {{ font-size: 19px; font-weight: 800; display: flex; align-items: center; gap: 10px; }}
        .badge-admin {{ font-size: 10.5px; background: var(--accent); color: #04261b; padding: 3px 8px; border-radius: 6px; font-weight: 800; text-transform: uppercase; }}
        .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; text-decoration: none; border: 1px solid transparent; cursor: pointer; }}
        .btn-p {{ background: var(--accent); color: #04261b; }}
        .btn-s {{ background: var(--surface-2); color: var(--text); border-color: var(--border); }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .stat-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; }}
        .stat-lbl {{ font-size: 11.5px; color: var(--muted); text-transform: uppercase; font-weight: 700; margin-bottom: 6px; }}
        .stat-val {{ font-size: 26px; font-weight: 800; }}
        .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 22px; margin-bottom: 24px; }}
        .card-title {{ font-size: 16px; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
        th {{ text-align: left; padding: 12px 14px; font-size: 11.5px; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 700; }}
        td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); }}
        tr:hover td {{ background: var(--surface-2); }}
        .table-wrap {{ overflow-x: auto; width: 100%; }}
        code {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="brand">
            <span>🛡️ FoodCheck Admin Console</span>
            <span class="badge-admin">Admin</span>
        </div>
        <div style="display: flex; gap: 10px; align-items: center;">
            <a href="/" class="btn btn-s">🌐 Web App ↗</a>
            <a href="/admin/dashboard" class="btn btn-p">🔄 Refresh</a>
            <a href="/admin/logout" class="btn btn-s" style="color: #f87171;">🚪 Sign Out</a>
        </div>
    </div>

    <div class="card" style="margin-bottom: 24px; background: linear-gradient(135deg, #121c27, #182535);">
        <div class="card-title">
            <span>🛡️ Current Administrator Profile</span>
            <span class="badge-admin">Authenticated</span>
        </div>
        <div style="display: flex; align-items: center; gap: 18px; flex-wrap: wrap;">
            <div style="width: 44px; height: 44px; border-radius: 50%; background: var(--accent); color: #04261b; display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 18px;">
                {cur_name[0].upper() if cur_name else 'A'}
            </div>
            <div>
                <div style="font-weight: 700; font-size: 15px;">{cur_name}</div>
                <div style="font-size: 12.5px; color: var(--muted);">User ID: <strong>@{cur_user}</strong> &bull; Email: <strong>{cur_email}</strong></div>
            </div>
            <div style="margin-left: auto;">
                <a href="/admin/keys" class="btn btn-s">🔑 Admin Invite Keys</a>
            </div>
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-lbl">Total Users</div>
            <div class="stat-val">{total_users}</div>
        </div>
        <div class="stat-card">
            <div class="stat-lbl">Total Product Scans</div>
            <div class="stat-val" style="color: var(--accent);">{total_scans}</div>
        </div>
        <div class="stat-card">
            <div class="stat-lbl">Diet Plans Generated</div>
            <div class="stat-val" style="color: var(--accent-2);">{diet_plans_count}</div>
        </div>
        <div class="stat-card">
            <div class="stat-lbl">Recent Login Events</div>
            <div class="stat-val">{len(recent_logins)}</div>
        </div>
    </div>

    <div class="card">
        <div class="card-title">
            <span>👥 User Directory &amp; Usage Details ({len(users)} Users)</span>
            <span style="font-size: 12px; color: var(--muted); font-weight: normal;">Live user accounts &amp; usage statistics</span>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>User ID</th>
                        <th>Full Name</th>
                        <th>Email ID</th>
                        <th>Role</th>
                        <th>Usage (How Much Is Using)</th>
                        <th>Status</th>
                        <th>Joined</th>
                        <th>Last Active</th>
                    </tr>
                </thead>
                <tbody>
                    {user_rows}
                </tbody>
            </table>
        </div>
    </div>

    <div class="card">
        <div class="card-title">
            <span>🔐 Recent Login Events (Who is Logging In)</span>
            <span style="font-size: 12px; color: var(--muted); font-weight: normal;">Latest authentication events</span>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>User ID / Attempted</th>
                        <th>Event Type</th>
                        <th>Status</th>
                        <th>IP Address</th>
                        <th>Device / User Agent</th>
                        <th>Time</th>
                    </tr>
                </thead>
                <tbody>
                    {login_rows}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""
    return render_template_string(html), 200


# ── DASHBOARD & USER ANALYTICS ──────────────────────────────────────────────

@admin_bp.route("/")
@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    five_min_ago = datetime.utcnow() - timedelta(minutes=5)

    # 1. KPI Counts (safe with fallback & rollback on error)
    total_users = 0
    new_users_today = 0
    users_online_now = 0
    total_scans = 0
    scans_today = 0
    diet_plans_count = 0
    logins_today = 0
    failed_logins_today = 0
    users_data = []
    user_scan_counts = {}
    user_diet_counts = {}
    recent_logins_data = []
    has_master_key = bool(User.get_master_admin_password())
    total_admin_keys = 0
    search_q = request.args.get("q", "").strip()

    try:
        try:
            total_users = User.query.count()
            new_users_today = User.query.filter(User.created_at >= today_start).count()
            users_online_now = User.query.filter(User.last_seen_at >= five_min_ago).count()
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Dashboard user stats warning: {e}")

        try:
            total_scans = Scan.query.count()
            scans_today = Scan.query.filter(Scan.created_at >= today_start).count()
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Dashboard scan stats warning: {e}")

        try:
            logins_today = LoginEvent.query.filter(
                LoginEvent.created_at >= today_start,
                LoginEvent.event_type.in_(["login", "admin_login"]),
                LoginEvent.success == True,
            ).count()
            failed_logins_today = LoginEvent.query.filter(
                LoginEvent.created_at >= today_start,
                LoginEvent.event_type == "failed_login",
            ).count()
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Dashboard login stats warning: {e}")

        try:
            diet_plans_count = get_total_diet_plans_count()
        except Exception as e:
            print(f"[!] Dashboard diet stats warning: {e}")

        # 3. Usage Counts Per User (Product scans & Diet plans)
        try:
            scan_counts_query = (
                db.session.query(Scan.user_id, func.count(Scan.id))
                .filter(Scan.user_id.isnot(None))
                .group_by(Scan.user_id)
                .all()
            )
            user_scan_counts = {uid: count for uid, count in scan_counts_query if uid}
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Scan counts per user warning: {e}")

        try:
            with DatabaseConnection() as (conn, is_pg):
                cursor = conn.cursor()
                cursor.execute("SELECT user_id, COUNT(*) FROM diet_plans GROUP BY user_id;")
                rows = cursor.fetchall()
                cursor.close()
                for r in rows:
                    if r and r[0]:
                        user_diet_counts[str(r[0]).strip().lower()] = r[1]
        except Exception as e:
            print(f"[!] Diet counts per user warning: {e}")

        # 2. Users Listing & Search - converted to robust in-memory dicts with preformatted dates
        try:
            user_query = User.query
            if search_q:
                like_pat = f"%{search_q}%"
                user_query = user_query.filter(
                    (User.username.ilike(like_pat))
                    | (User.name.ilike(like_pat))
                    | (User.email.ilike(like_pat))
                )
            raw_users = user_query.order_by(User.created_at.desc()).limit(100).all()
            for u in raw_users:
                uname_clean = str(u.username or "").strip().lower()
                is_u_admin = bool(getattr(u, "is_admin", False) or getattr(u, "role", "") in ("admin", "developer"))
                created_str = _safe_format_dt(getattr(u, "created_at", None), "%Y-%m-%d")
                last_seen = getattr(u, "last_seen_at", None)
                last_seen_str = _safe_format_dt(last_seen, "%Y-%m-%d %H:%M") if last_seen else "Never"
                users_data.append({
                    "id": u.id or 0,
                    "username": u.username or uname_clean,
                    "name": u.name or u.username or uname_clean,
                    "email": u.email or "",
                    "is_admin": is_u_admin,
                    "is_active": bool(getattr(u, "is_active", True)),
                    "is_locked": bool(getattr(u, "is_locked", False)),
                    "created_at_fmt": created_str,
                    "last_seen_fmt": last_seen_str,
                    "scans_count": user_scan_counts.get(u.id, user_scan_counts.get(uname_clean, 0)),
                    "diet_count": user_diet_counts.get(uname_clean, 0),
                })
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Dashboard users query warning: {e}")
            users_data = []

        # Fallback to legacy users_auth table if users_data is empty
        if not users_data:
            try:
                with DatabaseConnection() as (conn, is_pg):
                    cursor = conn.cursor()
                    cursor.execute("SELECT user_id, name, role, created_at FROM users_auth ORDER BY created_at DESC LIMIT 100;")
                    rows = cursor.fetchall()
                    cursor.close()
                    for r in rows:
                        if r and r[0]:
                            uid = str(r[0]).strip().lower()
                            users_data.append({
                                "id": 0,
                                "username": uid,
                                "name": r[1] or uid.capitalize(),
                                "email": "",
                                "is_admin": r[2] in ("admin", "developer"),
                                "is_active": True,
                                "is_locked": False,
                                "created_at_fmt": _safe_format_dt(r[3], "%Y-%m-%d"),
                                "last_seen_fmt": "Never",
                                "scans_count": user_scan_counts.get(uid, 0),
                                "diet_count": user_diet_counts.get(uid, 0),
                            })
            except Exception:
                pass

        if total_users == 0 and users_data:
            total_users = len(users_data)

        # 4. Recent Logins - pre-extracting data to avoid any lazy-load DB operations during template render
        try:
            raw_logins = (
                LoginEvent.query.order_by(LoginEvent.created_at.desc())
                .limit(35)
                .all()
            )
            for l in raw_logins:
                uname = None
                try:
                    if l.user:
                        uname = l.user.username
                except Exception:
                    pass
                if not uname:
                    uname = getattr(l, "user_id_attempted", None) or getattr(l, "email_attempted", None) or "Anonymous"
                time_str = _safe_format_dt(getattr(l, "created_at", None), "%Y-%m-%d %H:%M:%S")
                recent_logins_data.append({
                    "id": l.id or 0,
                    "username": uname,
                    "event_type": l.event_type or "login",
                    "success": bool(l.success),
                    "ip_address": l.ip_address or "—",
                    "user_agent": (l.user_agent or "—")[:80],
                    "time_fmt": time_str,
                })
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Recent logins warning: {e}")
            recent_logins_data = []

        # 5. Admin Details & Master Key status
        try:
            total_admin_keys = AdminInviteKey.query.filter_by(revoked=False).count()
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            print(f"[!] Admin keys count warning: {e}")

        return render_template(
            "admin/dashboard.html",
            total_users=total_users,
            new_users_today=new_users_today,
            users_online_now=users_online_now,
            total_scans=total_scans,
            scans_today=scans_today,
            diet_plans_count=diet_plans_count,
            logins_today=logins_today,
            failed_logins_today=failed_logins_today,
            users=users_data,
            user_scan_counts=user_scan_counts,
            user_diet_counts=user_diet_counts,
            recent_logins=recent_logins_data,
            has_master_key=has_master_key,
            total_admin_keys=total_admin_keys,
            search_q=search_q,
        )
    except Exception as render_err:
        print(f"[!] Critical dashboard render error: {render_err}")
        try:
            return render_template(
                "admin/dashboard.html",
                total_users=total_users if 'total_users' in locals() else 0,
                new_users_today=new_users_today if 'new_users_today' in locals() else 0,
                users_online_now=users_online_now if 'users_online_now' in locals() else 0,
                total_scans=total_scans if 'total_scans' in locals() else 0,
                scans_today=scans_today if 'scans_today' in locals() else 0,
                diet_plans_count=diet_plans_count if 'diet_plans_count' in locals() else 0,
                logins_today=logins_today if 'logins_today' in locals() else 0,
                failed_logins_today=failed_logins_today if 'failed_logins_today' in locals() else 0,
                users=users_data if 'users_data' in locals() else [],
                user_scan_counts=user_scan_counts if 'user_scan_counts' in locals() else {},
                user_diet_counts=user_diet_counts if 'user_diet_counts' in locals() else {},
                recent_logins=recent_logins_data if 'recent_logins_data' in locals() else [],
                has_master_key=bool(User.get_master_admin_password()),
                total_admin_keys=total_admin_keys if 'total_admin_keys' in locals() else 0,
                search_q="",
            )
        except Exception as e2:
            print(f"[!] Secondary dashboard fallback error: {e2}")
            return _render_standalone_compact_dashboard(
                users=users_data if 'users_data' in locals() else [],
                recent_logins=recent_logins_data if 'recent_logins_data' in locals() else [],
                total_users=total_users if 'total_users' in locals() else 0,
                total_scans=total_scans if 'total_scans' in locals() else 0,
                diet_plans_count=diet_plans_count if 'diet_plans_count' in locals() else 0,
                error_note=str(e2)
            )


# ── LIVE ACTIVITY FEED ──────────────────────────────────────────────────────

@admin_bp.route("/feed")
@admin_required
def feed_page():
    return render_template("admin/feed.html")


@admin_bp.route("/api/feed")
@admin_required
def api_feed():
    """Return the latest 50 activities and login events for auto-refresh."""
    activities = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(50).all()

    feed_items = []
    for a in activities:
        suspicious = False
        if a.user_id and a.action in ("user_login", "admin_login") and a.ip_address:
            # Check if this IP is unseen
            prev_count = LoginEvent.query.filter(
                LoginEvent.user_id == a.user_id,
                LoginEvent.ip_address == a.ip_address,
                LoginEvent.success == True
            ).count()
            suspicious = (prev_count <= 1)

        feed_items.append({
            "id": a.id,
            "user": a.user.username if a.user else "Anonymous",
            "user_id": a.user_id,
            "action": a.action,
            "details": a.details,
            "ip": a.ip_address or "Unknown",
            "path": a.path or "",
            "time": a.created_at.strftime("%H:%M:%S (%b %d)"),
            "suspicious": suspicious,
        })

    return jsonify({"feed": feed_items})


# ── USER MANAGEMENT & DETAIL ────────────────────────────────────────────────

@admin_bp.route("/users")
@admin_required
def users_list():
    page = request.args.get("page", 1, type=int)
    search_q = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = User.query

    if search_q:
        query = query.filter(
            (User.username.ilike(f"%{search_q}%")) |
            (User.email.ilike(f"%{search_q}%")) |
            (User.name.ilike(f"%{search_q}%"))
        )

    if role_filter in ("user", "admin"):
        query = query.filter(User.role == role_filter)

    if status_filter == "active":
        query = query.filter(User.is_active == True)
    elif status_filter == "disabled":
        query = query.filter(User.is_active == False)
    elif status_filter == "locked":
        query = query.filter(User.locked_until > datetime.utcnow())

    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=15, error_out=False)

    # Attach scan counts safely
    user_scan_counts = {}
    item_ids = [u.id for u in pagination.items if u and getattr(u, 'id', None)]
    if item_ids:
        try:
            user_scan_counts = dict(
                db.session.query(Scan.user_id, func.count(Scan.id))
                .filter(Scan.user_id.in_(item_ids))
                .group_by(Scan.user_id)
                .all()
            )
        except Exception as e:
            print(f"[!] User scan counts warning: {e}")

    return render_template(
        "admin/users.html",
        pagination=pagination,
        search_q=search_q,
        role_filter=role_filter,
        status_filter=status_filter,
        user_scan_counts=user_scan_counts,
    )


@admin_bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    recent_logins = LoginEvent.query.filter_by(user_id=user.id).order_by(LoginEvent.created_at.desc()).limit(15).all()
    recent_scans = Scan.query.filter_by(user_id=user.id).order_by(Scan.created_at.desc()).limit(15).all()
    active_sessions = UserSession.query.filter_by(user_id=user.id, revoked=False).order_by(UserSession.last_seen_at.desc()).all()

    return render_template(
        "admin/user_detail.html",
        user=user,
        recent_logins=recent_logins,
        recent_scans=recent_scans,
        active_sessions=active_sessions,
    )


@admin_bp.route("/users/<int:user_id>/action", methods=["POST"])
@admin_required
def user_action(user_id):
    target = db.session.get(User, user_id)
    back_url = request.referrer or url_for("admin.dashboard")
    if not target:
        flash("Target user account was not found.", "warning")
        return redirect(back_url)
    action = request.form.get("action")

    # Guard: Cannot modify self
    back_url = request.referrer or url_for("admin.dashboard")
    if target.id == current_user.id and action in ("disable", "delete"):
        flash("You cannot disable or delete your own administrator account.", "danger")
        return redirect(back_url)

    # Guard: Cannot delete/disable the last remaining admin
    if target.is_admin and action in ("disable", "delete"):
        active_admins = User.query.filter_by(role="admin", is_active=True).count()
        if active_admins <= 1:
            flash("Operation aborted: Cannot disable or delete the only remaining active administrator.", "danger")
            return redirect(back_url)

    if action == "disable":
        target.is_active = False
        db.session.commit()
        log_admin_audit(current_user.id, "disable_user", target.id, f"Disabled user '{target.username}'")
        flash(f"User '{target.username}' has been disabled.", "warning")

    elif action == "enable":
        target.is_active = True
        db.session.commit()
        log_admin_audit(current_user.id, "enable_user", target.id, f"Enabled user '{target.username}'")
        flash(f"User '{target.username}' has been enabled.", "success")

    elif action == "reset_lockout":
        target.reset_failed_logins()
        db.session.commit()
        log_admin_audit(current_user.id, "reset_lockout", target.id, f"Reset lockout on user '{target.username}'")
        flash(f"Lockout and failed attempts reset for '{target.username}'.", "success")

    elif action == "force_logout":
        UserSession.query.filter_by(user_id=target.id).update({"revoked": True})
        db.session.commit()
        log_admin_audit(current_user.id, "force_logout", target.id, f"Terminated all active sessions for '{target.username}'")
        flash(f"All active sessions for '{target.username}' have been revoked.", "info")

    elif action == "delete":
        username = target.username
        delete_user_data(username)
        db.session.delete(target)
        db.session.commit()
        log_admin_audit(current_user.id, "delete_user", details=f"Permanently deleted user '{username}' (ID {user_id}) and private diet records")
        flash(f"User '{username}' and all associated data were permanently deleted.", "info")
        return redirect(url_for("admin.users_list"))

    return redirect(back_url)


# ── SCANS EXPLORER ──────────────────────────────────────────────────────────

@admin_bp.route("/scans")
@admin_required
def scans_list():
    page = request.args.get("page", 1, type=int)
    verdict = request.args.get("verdict", "").strip()
    brand = request.args.get("brand", "").strip()
    success_filter = request.args.get("success", "").strip()
    username = request.args.get("username", "").strip()

    query = Scan.query

    if verdict:
        query = query.filter(Scan.health_verdict == verdict)
    if brand:
        query = query.filter(Scan.brand.ilike(f"%{brand}%"))
    if success_filter == "1":
        query = query.filter(Scan.success == True)
    elif success_filter == "0":
        query = query.filter(Scan.success == False)
    if username:
        user_match = User.query.filter_by(username=username).first()
        if user_match:
            query = query.filter(Scan.user_id == user_match.id)
        else:
            query = query.filter(Scan.id == -1)

    pagination = query.order_by(Scan.created_at.desc()).paginate(page=page, per_page=20, error_out=False)

    return render_template(
        "admin/scans.html",
        pagination=pagination,
        verdict=verdict,
        brand=brand,
        success_filter=success_filter,
        username=username,
    )


# ── LOGIN HISTORY & ACTIVE SESSIONS ─────────────────────────────────────────

@admin_bp.route("/logins")
@admin_required
def logins_history():
    page = request.args.get("page", 1, type=int)
    event_type = request.args.get("event_type", "").strip()
    username = request.args.get("username", "").strip()
    success_val = request.args.get("success", "").strip()

    query = LoginEvent.query

    if event_type:
        query = query.filter(LoginEvent.event_type == event_type)
    if username:
        u = User.query.filter_by(username=username).first()
        if u:
            query = query.filter(LoginEvent.user_id == u.id)
        else:
            query = query.filter(LoginEvent.email_attempted.ilike(f"%{username}%"))
    if success_val == "1":
        query = query.filter(LoginEvent.success == True)
    elif success_val == "0":
        query = query.filter(LoginEvent.success == False)

    pagination = query.order_by(LoginEvent.created_at.desc()).paginate(page=page, per_page=25, error_out=False)

    return render_template(
        "admin/logins.html",
        pagination=pagination,
        event_type=event_type,
        username=username,
        success_val=success_val,
    )


@admin_bp.route("/sessions")
@admin_required
def active_sessions():
    now_minus_30m = datetime.utcnow() - timedelta(minutes=30)
    sessions = (
        UserSession.query
        .filter(UserSession.revoked == False, UserSession.last_seen_at >= now_minus_30m)
        .order_by(UserSession.last_seen_at.desc())
        .all()
    )
    return render_template("admin/sessions.html", sessions=sessions)


@admin_bp.route("/sessions/<int:session_id>/revoke", methods=["POST"])
@admin_required
def revoke_session(session_id):
    sess = UserSession.query.get_or_404(session_id)
    sess.revoked = True
    db.session.commit()
    log_admin_audit(
        current_user.id,
        "revoke_session",
        sess.user_id,
        f"Revoked session #{session_id} for user '{sess.user.username}'",
    )
    flash("Session terminated successfully.", "info")
    return redirect(url_for("admin.active_sessions"))


# ── ADMIN KEYS PAGE ─────────────────────────────────────────────────────────

@admin_bp.route("/keys")
@admin_required
def keys_page():
    keys = AdminInviteKey.query.order_by(AdminInviteKey.created_at.desc()).all()
    return render_template("admin/keys.html", keys=keys)


@admin_bp.route("/keys/<int:key_id>/revoke", methods=["POST"])
@admin_required
def revoke_key(key_id):
    key = AdminInviteKey.query.get_or_404(key_id)
    key.revoked = True
    db.session.commit()
    log_admin_audit(current_user.id, "revoke_admin_key", details=f"Revoked invite key #{key.id}")
    flash(f"Admin Invite Key #{key_id} has been revoked.", "warning")
    return redirect(url_for("admin.keys_page"))


# ── SYSTEM HEALTH & ERROR VIEWER ────────────────────────────────────────────

@admin_bp.route("/health")
@admin_required
def system_health():
    errors = ErrorLog.query.order_by(ErrorLog.created_at.desc()).limit(50).all()

    # Gemini API metrics
    gemini_total = Scan.query.filter(Scan.gemini_used == True).count()
    gemini_success = Scan.query.filter(Scan.gemini_used == True, Scan.success == True).count()
    gemini_failed = Scan.query.filter(Scan.gemini_used == True, Scan.success == False).count()
    gemini_avg_time = 0.0
    try:
        raw_avg = (
            db.session.query(func.avg(Scan.response_time_ms))
            .filter(Scan.gemini_used == True, Scan.response_time_ms.isnot(None))
            .scalar()
        )
        if raw_avg is not None:
            gemini_avg_time = round(float(raw_avg), 1)
    except Exception as e:
        print(f"[!] Health gemini avg time warning: {e}")

    # Open Food Facts metrics
    off_hits = Scan.query.filter(Scan.barcode.isnot(None)).count()
    off_misses = ErrorLog.query.filter(ErrorLog.source == "open_food_facts").count()

    return render_template(
        "admin/health.html",
        errors=errors,
        gemini_total=gemini_total,
        gemini_success=gemini_success,
        gemini_failed=gemini_failed,
        gemini_avg_time=round(gemini_avg_time, 1),
        off_hits=off_hits,
        off_misses=off_misses,
    )


# ── AUDIT LOG ───────────────────────────────────────────────────────────────

@admin_bp.route("/audit")
@admin_required
def audit_log():
    page = request.args.get("page", 1, type=int)
    logs = AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template("admin/audit.html", logs=logs)


# ── DATA EXPORT (CSV) ───────────────────────────────────────────────────────

@admin_bp.route("/export", methods=["GET", "POST"])
@admin_required
def export_data():
    form = AdminExportForm()
    if form.validate_on_submit():
        dataset = form.export_type.data
        date_from_str = form.date_from.data
        date_to_str = form.date_to.data

        output = io.StringIO()
        writer = csv.writer(output)

        if dataset == "users":
            writer.writerow(["ID", "Username", "Email", "Name", "Role", "Active", "Created At", "Last Login", "Login Count"])
            query = User.query
            if date_from_str:
                query = query.filter(User.created_at >= date_from_str)
            if date_to_str:
                query = query.filter(User.created_at <= f"{date_to_str} 23:59:59")
            for u in query.order_by(User.id).all():
                writer.writerow([u.id, u.username, u.email or "", u.name, u.role, u.is_active, u.created_at, u.last_login_at, u.login_count])
            filename = f"foodcheck_users_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        elif dataset == "scans":
            writer.writerow(["ID", "User ID", "Product Name", "Brand", "Verdict", "Score", "Source", "Gemini Used", "Success", "Response Time (ms)", "Created At"])
            query = Scan.query
            if date_from_str:
                query = query.filter(Scan.created_at >= date_from_str)
            if date_to_str:
                query = query.filter(Scan.created_at <= f"{date_to_str} 23:59:59")
            for s in query.order_by(Scan.id).all():
                writer.writerow([s.id, s.user_id or "anon", s.product_name, s.brand, s.health_verdict, s.health_score, s.source, s.gemini_used, s.success, s.response_time_ms, s.created_at])
            filename = f"foodcheck_scans_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        elif dataset == "login_events":
            writer.writerow(["ID", "User ID", "Attempted", "Event Type", "IP Address", "User Agent", "Success", "Created At"])
            query = LoginEvent.query
            if date_from_str:
                query = query.filter(LoginEvent.created_at >= date_from_str)
            if date_to_str:
                query = query.filter(LoginEvent.created_at <= f"{date_to_str} 23:59:59")
            for e in query.order_by(LoginEvent.id).all():
                writer.writerow([e.id, e.user_id or "", e.email_attempted, e.event_type, e.ip_address, e.user_agent, e.success, e.created_at])
            filename = f"foodcheck_logins_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        elif dataset == "feedbacks":
            writer.writerow(["ID", "User ID", "Name", "Rating", "Message", "Hidden", "Created At"])
            feedbacks_all = get_all_feedbacks(include_hidden=True)
            for fb in feedbacks_all:
                created = str(fb.get("created_at") or fb.get("date") or "")
                if date_from_str and created < date_from_str:
                    continue
                if date_to_str and created > f"{date_to_str} 23:59:59":
                    continue
                writer.writerow([
                    fb.get("id"),
                    fb.get("user_id"),
                    fb.get("name"),
                    fb.get("rating"),
                    fb.get("message"),
                    "Yes" if fb.get("is_hidden") else "No",
                    created,
                ])
            filename = f"foodcheck_feedbacks_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        else:
            writer.writerow(["ID", "Source", "Message", "Path", "Created At"])
            query = ErrorLog.query
            if date_from_str:
                query = query.filter(ErrorLog.created_at >= date_from_str)
            if date_to_str:
                query = query.filter(ErrorLog.created_at <= f"{date_to_str} 23:59:59")
            for err in query.order_by(ErrorLog.id).all():
                writer.writerow([err.id, err.source, err.message, err.path, err.created_at])
            filename = f"foodcheck_errors_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        output.seek(0)
        log_admin_audit(current_user.id, "csv_export", details=f"Downloaded CSV export for '{dataset}'")
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    return render_template("admin/export.html", form=form)


# ── SETTINGS MANAGEMENT ─────────────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    form = AdminSettingsForm()
    if request.method == "GET":
        form.maintenance_mode.data = AppSetting.get_setting("maintenance_mode", "false") == "true"
        form.registration_enabled.data = AppSetting.get_setting("registration_enabled", "true") == "true"
        form.announcement_banner.data = AppSetting.get_setting("announcement_banner", "")
        form.daily_scan_limit_anon.data = int(AppSetting.get_setting("daily_scan_limit_anon", "25"))
        form.daily_scan_limit_user.data = int(AppSetting.get_setting("daily_scan_limit_user", "100"))

    elif form.validate_on_submit():
        AppSetting.set_setting("maintenance_mode", "true" if form.maintenance_mode.data else "false", "Maintenance mode active")
        AppSetting.set_setting("registration_enabled", "true" if form.registration_enabled.data else "false", "Public registration toggle")
        AppSetting.set_setting("announcement_banner", form.announcement_banner.data or "", "Banner announcement")
        AppSetting.set_setting("daily_scan_limit_anon", str(form.daily_scan_limit_anon.data), "Daily limit per anonymous IP")
        AppSetting.set_setting("daily_scan_limit_user", str(form.daily_scan_limit_user.data), "Daily limit per signed-in user")

        log_admin_audit(current_user.id, "update_settings", details="Updated system configuration settings")
        flash("System settings saved successfully.", "success")
        return redirect(url_for("admin.settings_page"))

    return render_template("admin/settings.html", form=form)


# ── REVIEWS MODERATION ───────────────────────────────────────────────────────

@admin_bp.route("/reviews")
@admin_required
def reviews_list():
    q = (request.args.get("q") or "").strip().lower()
    feedbacks = get_all_feedbacks(include_hidden=True)
    if q:
        feedbacks = [
            f for f in feedbacks
            if q in (f.get("name") or "").lower()
            or q in (f.get("message") or "").lower()
            or q in (f.get("user_id") or "").lower()
        ]
    return render_template("admin/reviews.html", feedbacks=feedbacks, q=q)


@admin_bp.route("/reviews/<fb_id>/toggle-hide", methods=["POST"])
@admin_required
def toggle_hide_review_action(fb_id):
    hide_val = request.form.get("hide") == "true"
    success = hide_feedback_db(fb_id, hide=hide_val)
    action_name = "hide_review" if hide_val else "restore_review"
    if success:
        log_admin_audit(current_user.id, action_name, details=f"Review '{fb_id}' set to {'hidden' if hide_val else 'visible'}")
        flash(f"Review '{fb_id}' has been {'hidden' if hide_val else 'restored'}.", "success")
    else:
        flash(f"Review '{fb_id}' could not be updated.", "warning")
    return redirect(url_for("admin.reviews_list"))


@admin_bp.route("/reviews/<fb_id>/delete", methods=["POST"])
@admin_required
def delete_review_action(fb_id):
    success = delete_feedback_db(fb_id, is_admin=True)
    if success:
        log_admin_audit(current_user.id, "delete_review", details=f"Deleted community review '{fb_id}'")
        flash(f"Review '{fb_id}' deleted successfully.", "success")
    else:
        flash(f"Review '{fb_id}' could not be found or was already deleted.", "warning")
    return redirect(url_for("admin.reviews_list"))
