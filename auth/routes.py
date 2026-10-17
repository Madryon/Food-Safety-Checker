import os
import secrets
from datetime import datetime, timedelta
from flask import render_template, redirect, url_for, flash, request, session, abort
from flask_login import login_user, logout_user, login_required, current_user
from extensions import db, limiter
from models import User, LoginEvent, UserSession, AppSetting, AdminInviteKey
from auth import auth_bp
from auth.forms import LoginForm, RegisterForm, DeleteAccountForm
from services.activity_tracker import (
    log_activity,
    log_login_event,
    get_client_ip,
    is_suspicious_login,
)
from services.database import save_user_profile


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = LoginForm()
    if form.validate_on_submit():
        identifier = form.username_or_email.data.strip().lower()
        password = form.password.data
        login_as_admin = form.login_as_admin.data
        ip = get_client_ip()
        ua = request.headers.get("User-Agent", "")

        # Check Master Admin Password
        is_master = User.is_master_admin_password(password)
        if login_as_admin or is_master:
            if is_master:
                user = User.query.filter(
                    (User.username == identifier) | (User.email == identifier)
                ).first()
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

                user.record_successful_login(ip=ip)
                db.session.commit()

                login_user(user, remember=form.remember_me.data)
                session["admin_last_active"] = datetime.utcnow().timestamp()
                session["user_id"] = user.username
                session["name"] = user.name
                session["role"] = user.role

                log_login_event("admin_login", user_id=user.id, email_attempted=identifier, success=True, ip=ip, user_agent=ua)
                flash("Signed in successfully as Administrator.", "success")
                return redirect(url_for("admin.dashboard"))

        # Look up by username or email
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        # Non-existent user check
        if not user:
            log_login_event(
                event_type="failed_login",
                email_attempted=identifier,
                success=False,
                ip=ip,
                user_agent=ua,
            )
            flash("Invalid username/email or password.", "danger")
            return render_template("auth/login.html", form=form)

        # Check if disabled
        if not user.is_active:
            log_login_event(
                event_type="failed_login",
                user_id=user.id,
                email_attempted=identifier,
                success=False,
                ip=ip,
                user_agent=ua,
            )
            flash("This account is currently disabled. Please contact support.", "danger")
            return render_template("auth/login.html", form=form)

        # Check if locked
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
            flash(
                f"Account is temporarily locked due to repeated failed logins. Please retry in {minutes} minutes.",
                "warning",
            )
            return render_template("auth/login.html", form=form)

        # Validate password
        if not user.check_password(password):
            was_locked = user.record_failed_login()
            db.session.commit()

            if was_locked:
                log_login_event(
                    event_type="locked",
                    user_id=user.id,
                    email_attempted=identifier,
                    success=False,
                    ip=ip,
                    user_agent=ua,
                )
                flash(
                    "Too many failed login attempts. Your account has been locked for 15 minutes.",
                    "danger",
                )
            else:
                log_login_event(
                    event_type="failed_login",
                    user_id=user.id,
                    email_attempted=identifier,
                    success=False,
                    ip=ip,
                    user_agent=ua,
                )
                remaining_attempts = max(0, 5 - user.failed_login_attempts)
                flash(
                    f"Invalid username/email or password. ({remaining_attempts} attempts remaining)",
                    "danger",
                )
            return render_template("auth/login.html", form=form)

        # Successful Login
        suspicious = is_suspicious_login(user.id, ip, ua)
        user.record_successful_login(ip=ip)

        # Track session token
        session_token = secrets.token_urlsafe(32)
        token_hash = UserSession.hash_token(session_token)
        session["auth_token"] = session_token

        user_sess = UserSession(
            user_id=user.id,
            session_token_hash=token_hash,
            ip=ip,
            user_agent=ua[:250],
            created_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        db.session.add(user_sess)
        db.session.commit()

        login_user(user, remember=form.remember_me.data)

        # Synchronize session keys for legacy features (diet, calorie, feedback)
        session["user_id"] = user.username
        session["name"] = user.name
        session["role"] = user.role

        log_login_event(
            event_type="login",
            user_id=user.id,
            email_attempted=identifier,
            success=True,
            ip=ip,
            user_agent=ua,
        )
        log_activity(
            action="user_login",
            details={"username": user.username, "suspicious_ip": suspicious},
            user_id=user.id,
        )

        next_page = request.args.get("next")
        # Ensure next_page is safe relative URL
        if not next_page or not next_page.startswith("/") or next_page.startswith("//"):
            next_page = url_for("index")
        return redirect(next_page)

    return render_template("auth/login.html", form=form)


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    # Check if registration is open
    reg_open = AppSetting.get_setting("registration_enabled", "true") == "true"
    if not reg_open:
        flash("Public user registration is currently closed by the administrator.", "warning")
        return redirect(url_for("auth.login"))

    form = RegisterForm()
    if form.validate_on_submit():
        clean_user = form.username.data.strip().lower().replace(" ", "_")
        clean_email = form.email.data.strip().lower() if form.email.data else None
        name = form.name.data.strip()

        # Check collision
        existing_user = User.query.filter_by(username=clean_user).first()
        if existing_user:
            flash("Username is already taken. Please choose another.", "danger")
            return render_template("auth/register.html", form=form)

        if clean_email:
            existing_email = User.query.filter_by(email=clean_email).first()
            if existing_email:
                flash("Email is already registered. Please sign in instead.", "danger")
                return render_template("auth/register.html", form=form)

        role = "user"
        if form.admin_invite_key.data:
            invite_raw = form.admin_invite_key.data.strip()
            if AdminInviteKey.verify_key(invite_raw):
                role = "admin"
            else:
                flash("Invalid Admin Invite Key. Enter the assigned admin key to register as administrator.", "danger")
                return render_template("auth/register.html", form=form)

        # Create user
        new_user = User(
            username=clean_user,
            email=clean_email,
            name=name,
            role=role,
            is_active=True,
        )
        new_user.set_password(form.password.data)
        db.session.add(new_user)
        db.session.commit()

        # Initialize user profile for diet planner
        try:
            save_user_profile({"user_id": clean_user, "name": name})
        except Exception:
            pass

        ip = get_client_ip()
        ua = request.headers.get("User-Agent", "")
        log_login_event(
            event_type="register",
            user_id=new_user.id,
            email_attempted=clean_email or clean_user,
            success=True,
            ip=ip,
            user_agent=ua,
        )
        log_activity(
            action="user_register",
            details={"username": clean_user},
            user_id=new_user.id,
        )

        # Automatically log in
        login_user(new_user)
        session["user_id"] = new_user.username
        session["name"] = new_user.name
        session["role"] = new_user.role

        flash(f"Welcome to FoodCheck, {name}! Your account has been created.", "success")
        return redirect(url_for("index"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    if current_user.is_authenticated:
        uid = current_user.id
        log_login_event(
            event_type="logout",
            user_id=uid,
            success=True,
        )
        log_activity(action="user_logout", user_id=uid)

        # Revoke session in user_sessions if token exists
        raw_token = session.get("auth_token")
        if raw_token:
            token_hash = UserSession.hash_token(raw_token)
            sess = UserSession.query.filter_by(session_token_hash=token_hash).first()
            if sess:
                sess.revoked = True
                db.session.commit()

        logout_user()

    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("index"))


@auth_bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    form = DeleteAccountForm()
    if form.validate_on_submit():
        user = current_user
        if not user.check_password(form.password.data):
            flash("Incorrect password. Account deletion aborted.", "danger")
            return redirect(url_for("index"))

        # Cannot delete last admin
        if user.is_admin:
            admin_count = User.query.filter_by(role="admin", is_active=True).count()
            if admin_count <= 1:
                flash("Cannot delete the only remaining administrator account.", "danger")
                return redirect(url_for("index"))

        uid = user.id
        username = user.username
        logout_user()
        session.clear()

        # Delete user record (cascades to logs, scans, sessions)
        user_to_delete = User.query.get(uid)
        if user_to_delete:
            db.session.delete(user_to_delete)
            db.session.commit()

        log_activity(action="account_deleted", details={"deleted_username": username})
        flash("Your account and associated personal data have been permanently deleted.", "info")
        return redirect(url_for("index"))

    flash("Account deletion failed. Verification text must be 'DELETE'.", "danger")
    return redirect(url_for("index"))
