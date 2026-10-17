import os
import csv
import io
import json
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

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        # Generic error message to never disclose user existence or role
        generic_err = "Invalid administrator credentials or access denied."

        # Check Master Admin Password
        if User.is_master_admin_password(password):
            if not user:
                clean_user = identifier.replace(" ", "_").split("@")[0]
                user = User(
                    username=clean_user,
                    name=clean_user.capitalize(),
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
        else:
            if not user or user.role != "admin":
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
            key_hash = AdminInviteKey.hash_key(raw_key)
            invite = AdminInviteKey.query.filter_by(key_hash=key_hash).first()
            if not invite or not invite.is_valid():
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

        # Mark invite key used if it was a DB single-use key
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
        key_detail = f"Invite Key #{invite.id}" if invite else "Assigned Master Admin Key"
        log_admin_audit(
            admin_id=admin.id,
            action="admin_registered",
            details=f"Admin account registered via {key_detail}",
        )

        flash("Administrator registered successfully! Please log in.", "success")
        return redirect(url_for("admin.admin_login"))

    return render_template("admin/register.html", form=form)


# ── DASHBOARD & KPI ANALYTICS ───────────────────────────────────────────────

@admin_bp.route("/")
@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    five_min_ago = datetime.utcnow() - timedelta(minutes=5)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    # KPI stats
    total_users = User.query.count()
    new_users_today = User.query.filter(User.created_at >= today_start).count()
    users_online_now = User.query.filter(User.last_seen_at >= five_min_ago).count()
    logins_today = LoginEvent.query.filter(
        LoginEvent.created_at >= today_start,
        LoginEvent.event_type.in_(["login", "admin_login"]),
        LoginEvent.success == True
    ).count()
    failed_logins_today = LoginEvent.query.filter(
        LoginEvent.created_at >= today_start,
        LoginEvent.event_type == "failed_login"
    ).count()
    total_scans = Scan.query.count()
    scans_today = Scan.query.filter(Scan.created_at >= today_start).count()

    # 30-day scans per day
    daily_scans = (
        db.session.query(
            func.date(Scan.created_at).label("scan_date"),
            func.count(Scan.id).label("count")
        )
        .filter(Scan.created_at >= thirty_days_ago)
        .group_by("scan_date")
        .order_by("scan_date")
        .all()
    )
    chart_scans_labels = [str(r.scan_date) for r in daily_scans]
    chart_scans_data = [r.count for r in daily_scans]

    # 30-day signups per day
    daily_signups = (
        db.session.query(
            func.date(User.created_at).label("reg_date"),
            func.count(User.id).label("count")
        )
        .filter(User.created_at >= thirty_days_ago)
        .group_by("reg_date")
        .order_by("reg_date")
        .all()
    )
    chart_signups_labels = [str(r.reg_date) for r in daily_signups]
    chart_signups_data = [r.count for r in daily_signups]

    # Health verdict distribution
    verdict_counts = (
        db.session.query(Scan.health_verdict, func.count(Scan.id))
        .filter(Scan.health_verdict.isnot(None))
        .group_by(Scan.health_verdict)
        .all()
    )
    verdict_map = {"safe": 0, "moderate": 0, "unsafe": 0}
    for v, c in verdict_counts:
        v_clean = (v or "").lower().replace("healthy", "safe").replace("unhealthy", "unsafe")
        if v_clean in verdict_map:
            verdict_map[v_clean] += c

    # Top 10 scanned products
    top_products = (
        db.session.query(Scan.product_name, func.count(Scan.id).label("c"))
        .filter(Scan.product_name.isnot(None), Scan.product_name != "Unknown Product")
        .group_by(Scan.product_name)
        .order_by(desc("c"))
        .limit(10)
        .all()
    )

    # Top 10 brands
    top_brands = (
        db.session.query(Scan.brand, func.count(Scan.id).label("c"))
        .filter(Scan.brand.isnot(None), Scan.brand != "Unknown", Scan.brand != "Unknown Brand")
        .group_by(Scan.brand)
        .order_by(desc("c"))
        .limit(10)
        .all()
    )

    # Leaderboard: Most Unhealthy Products
    unhealthy_products = (
        db.session.query(
            Scan.product_name,
            Scan.brand,
            func.count(Scan.id).label("times_flagged"),
            func.avg(Scan.health_score).label("avg_score")
        )
        .filter(
            Scan.health_verdict.in_(["unsafe", "unhealthy", "moderate"]),
            Scan.product_name.isnot(None),
            Scan.product_name != "Unknown Product"
        )
        .group_by(Scan.product_name, Scan.brand)
        .order_by(desc("times_flagged"))
        .limit(5)
        .all()
    )

    # Leaderboard: Most Controversial Brands
    controversial_brands = (
        db.session.query(
            Scan.brand,
            func.count(Scan.id).label("flagged_count"),
            func.avg(Scan.health_score).label("avg_score")
        )
        .filter(
            Scan.health_verdict.in_(["unsafe", "unhealthy"]),
            Scan.brand.isnot(None),
            Scan.brand != "Unknown"
        )
        .group_by(Scan.brand)
        .order_by(desc("flagged_count"))
        .limit(5)
        .all()
    )

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        new_users_today=new_users_today,
        users_online_now=users_online_now,
        logins_today=logins_today,
        failed_logins_today=failed_logins_today,
        total_scans=total_scans,
        scans_today=scans_today,
        chart_scans_labels=json.dumps(chart_scans_labels),
        chart_scans_data=json.dumps(chart_scans_data),
        chart_signups_labels=json.dumps(chart_signups_labels),
        chart_signups_data=json.dumps(chart_signups_data),
        chart_verdicts_data=json.dumps([verdict_map["safe"], verdict_map["moderate"], verdict_map["unsafe"]]),
        chart_top_products_labels=json.dumps([p[0][:20] for p in top_products]),
        chart_top_products_data=json.dumps([p[1] for p in top_products]),
        chart_top_brands_labels=json.dumps([b[0][:20] for b in top_brands]),
        chart_top_brands_data=json.dumps([b[1] for b in top_brands]),
        unhealthy_products=unhealthy_products,
        controversial_brands=controversial_brands,
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

    # Attach scan counts
    user_scan_counts = dict(
        db.session.query(Scan.user_id, func.count(Scan.id))
        .filter(Scan.user_id.in_([u.id for u in pagination.items]))
        .group_by(Scan.user_id)
        .all()
    )

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
    target = User.query.get_or_404(user_id)
    action = request.form.get("action")

    # Guard: Cannot modify self
    if target.id == current_user.id and action in ("disable", "delete"):
        flash("You cannot disable or delete your own administrator account.", "danger")
        return redirect(url_for("admin.user_detail", user_id=target.id))

    # Guard: Cannot delete/disable the last remaining admin
    if target.is_admin and action in ("disable", "delete"):
        active_admins = User.query.filter_by(role="admin", is_active=True).count()
        if active_admins <= 1:
            flash("Operation aborted: Cannot disable or delete the only remaining active administrator.", "danger")
            return redirect(url_for("admin.user_detail", user_id=target.id))

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
        db.session.delete(target)
        db.session.commit()
        log_admin_audit(current_user.id, "delete_user", details=f"Permanently deleted user '{username}' (ID {user_id})")
        flash(f"User '{username}' was permanently deleted.", "info")
        return redirect(url_for("admin.users_list"))

    return redirect(url_for("admin.user_detail", user_id=target.id))


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
    gemini_avg_time = (
        db.session.query(func.avg(Scan.response_time_ms))
        .filter(Scan.gemini_used == True, Scan.response_time_ms.isnot(None))
        .scalar()
    ) or 0

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
