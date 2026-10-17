import secrets
import click
from datetime import datetime, timedelta
from extensions import db
from models import AdminInviteKey, User, ActivityLog, LoginEvent, Scan, ErrorLog, AdminAuditLog


def register_cli_commands(app):
    """Register custom Flask CLI management commands."""

    @app.cli.command("create-admin-key")
    @click.option("--expires-hours", default=24, type=int, help="Hours until the invite key expires.")
    def create_admin_key(expires_hours):
        """Generate a single-use Admin Invite Key. Prints the raw key once."""
        raw_key = secrets.token_urlsafe(32)
        key_hash = AdminInviteKey.hash_key(raw_key)
        expires_at = datetime.utcnow() + timedelta(hours=expires_hours)

        invite = AdminInviteKey(
            key_hash=key_hash,
            created_at=datetime.utcnow(),
            expires_at=expires_at,
            revoked=False,
        )
        db.session.add(invite)
        db.session.commit()

        click.echo("=" * 60)
        click.echo("[*] NEW ADMIN INVITE KEY GENERATED [*]")
        click.echo("=" * 60)
        click.echo(f"Key ID:       {invite.id}")
        click.echo(f"Invite Key:   {raw_key}")
        click.echo(f"Expires:      {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')} ({expires_hours} hours)")
        click.echo("=" * 60)
        click.echo("[!] SAVE THIS KEY NOW! It is hashed and can NEVER be shown again.")
        click.echo("The admin can register at /admin/register using this key.")
        click.echo("=" * 60)

    @app.cli.command("list-admin-keys")
    def list_admin_keys():
        """List all admin invite keys and their current statuses."""
        keys = AdminInviteKey.query.order_by(AdminInviteKey.created_at.desc()).all()
        if not keys:
            click.echo("No admin invite keys found.")
            return

        click.echo(f"{'ID':<4} {'Status':<10} {'Created':<20} {'Expires':<20} {'Used By':<15}")
        click.echo("-" * 75)
        for k in keys:
            if k.revoked:
                status = "REVOKED"
            elif k.used_at:
                status = "USED"
            elif k.expires_at < datetime.utcnow():
                status = "EXPIRED"
            else:
                status = "ACTIVE"

            used_by = k.used_by_user.username if k.used_by_user else "-"
            click.echo(
                f"{k.id:<4} {status:<10} {k.created_at.strftime('%Y-%m-%d %H:%M'):<20} "
                f"{k.expires_at.strftime('%Y-%m-%d %H:%M'):<20} {used_by:<15}"
            )

    @app.cli.command("revoke-admin-key")
    @click.argument("key_id", type=int)
    def revoke_admin_key(key_id):
        """Revoke an active admin invite key."""
        key = AdminInviteKey.query.get(key_id)
        if not key:
            click.echo(f"Error: Admin invite key with ID {key_id} does not exist.")
            return

        if key.revoked:
            click.echo(f"Key #{key_id} is already revoked.")
            return

        key.revoked = True
        db.session.commit()
        click.echo(f"[+] Admin invite key #{key_id} has been revoked successfully.")

    @app.cli.command("create-first-admin")
    @click.option("--username", prompt="Admin username", help="Unique username for the administrator.")
    @click.option("--email", prompt="Admin email", help="Administrator email address.")
    @click.option("--name", prompt="Full name", default="System Administrator", help="Display name.")
    @click.password_option("--password", prompt="Admin password", help="Secure password.")
    def create_first_admin(username, email, name, password):
        """Bootstrap the initial administrator account directly from CLI."""
        clean_user = username.strip().lower()
        clean_email = email.strip().lower()

        existing = User.query.filter(
            (User.username == clean_user) | (User.email == clean_email)
        ).first()
        if existing:
            click.echo(f"Error: A user with username '{clean_user}' or email '{clean_email}' already exists.")
            return

        if len(password) < 8:
            click.echo("Error: Password must be at least 8 characters long.")
            return

        admin = User(
            username=clean_user,
            email=clean_email,
            name=name.strip(),
            role="admin",
            is_active=True,
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        click.echo(f"[+] Administrator account '{clean_user}' created successfully! You can log in at /admin/login.")

    @app.cli.command("purge-old-logs")
    @click.option("--days", default=90, type=int, help="Delete logs older than N days (default 90).")
    def purge_old_logs(days):
        """Purge system activity, error, and login logs older than specified days."""
        cutoff = datetime.utcnow() - timedelta(days=days)

        act_del = ActivityLog.query.filter(ActivityLog.created_at < cutoff).delete()
        log_del = LoginEvent.query.filter(LoginEvent.created_at < cutoff).delete()
        err_del = ErrorLog.query.filter(ErrorLog.created_at < cutoff).delete()
        audit_del = AdminAuditLog.query.filter(AdminAuditLog.created_at < cutoff).delete()

        db.session.commit()
        click.echo(f"[+] Purged logs older than {days} days ({cutoff.strftime('%Y-%m-%d')}):")
        click.echo(f"  * Activity logs deleted: {act_del}")
        click.echo(f"  * Login events deleted:  {log_del}")
        click.echo(f"  * Error logs deleted:     {err_del}")
        click.echo(f"  * Audit logs deleted:     {audit_del}")
