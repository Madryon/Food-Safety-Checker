import os
import io
import json
import secrets
import unittest
from datetime import datetime, timedelta
from app import app
from extensions import db
from models import (
    User,
    AdminInviteKey,
    Scan,
    LoginEvent,
    ActivityLog,
    UserSession,
    AppSetting,
)


class AdminAndRoleAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        os.environ["ADMIN_MASTER_KEY"] = os.environ.get("ADMIN_MASTER_KEY", "TestAdminMasterKey2026!")
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["RATELIMIT_ENABLED"] = False
        app.config["RECAPTCHA_ENABLED"] = False
        from extensions import limiter
        limiter.enabled = False
        with app.app_context():
            db.drop_all()
            db.create_all()

    def setUp(self):
        self.app = app
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_01_user_password_policy(self):
        """Enforce at least 8 chars with at least one letter and one number."""
        # Too short (< 8)
        res_short = self.client.post("/register", data={
            "name": "Test Short",
            "username": "short_user",
            "password": "p1",
            "confirm_password": "p1",
        })
        self.assertIn(b"at least 8 characters", res_short.data)

        # No digits
        res_no_num = self.client.post("/register", data={
            "name": "Test No Num",
            "username": "nonum_user",
            "password": "passwordonly",
            "confirm_password": "passwordonly",
        })
        self.assertIn(b"at least one number", res_no_num.data)

        # No letters
        res_no_char = self.client.post("/register", data={
            "name": "Test No Char",
            "username": "nochar_user",
            "password": "1234567890",
            "confirm_password": "1234567890",
        })
        self.assertIn(b"at least one letter", res_no_char.data)

        # Valid registration
        res_valid = self.client.post("/register", data={
            "name": "Valid Member",
            "username": "valid_user_99",
            "email": "valid99@example.com",
            "password": "ValidPassword123",
            "confirm_password": "ValidPassword123",
        }, follow_redirects=True)
        self.assertEqual(res_valid.status_code, 200)

        # Verify role is STRICTLY 'user'
        u = User.query.filter_by(username="valid_user_99").first()
        self.assertIsNotNone(u)
        self.assertEqual(u.role, "user")

    def test_02_admin_panel_obscurity_returns_404(self):
        """Non-admins and anonymous visitors receive 404 to hide admin panel existence."""
        # Anonymous request to /admin/dashboard
        anon_res = self.client.get("/admin/dashboard")
        self.assertEqual(anon_res.status_code, 404)

        # Standard user request to /admin/dashboard
        with self.client:
            self.client.post("/login", data={
                "username_or_email": "valid_user_99",
                "password": "ValidPassword123",
            })
            user_res = self.client.get("/admin/dashboard")
            self.assertEqual(user_res.status_code, 404)

    def test_03_admin_developer_key_registration_flow(self):
        """Admin registration requires a valid developer invite key."""
        raw_key = "secret-invite-token-abc123xyz"
        key_hash = AdminInviteKey.hash_key(raw_key)

        invite = AdminInviteKey(
            key_hash=key_hash,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=24),
            revoked=False,
        )
        db.session.add(invite)
        db.session.commit()

        # Attempt with wrong key
        res_bad_key = self.client.post("/admin/register", data={
            "name": "Hacker",
            "username": "bad_admin",
            "email": "hacker@test.com",
            "password": "AdminPassword123",
            "confirm_password": "AdminPassword123",
            "invite_key": "wrong-key-value",
        })
        self.assertIn(b"Invalid, expired, or already-used Admin Invite Key", res_bad_key.data)

        # Attempt with valid key
        res_ok = self.client.post("/admin/register", data={
            "name": "Super Admin",
            "username": "super_admin_01",
            "email": "admin01@foodcheck.com",
            "password": "AdminPassword123",
            "confirm_password": "AdminPassword123",
            "invite_key": raw_key,
        }, follow_redirects=True)
        self.assertEqual(res_ok.status_code, 200)

        # Verify admin created
        admin = User.query.filter_by(username="super_admin_01").first()
        self.assertIsNotNone(admin)
        self.assertEqual(admin.role, "admin")

        # Verify key marked as used
        reloaded_invite = AdminInviteKey.query.get(invite.id)
        self.assertIsNotNone(reloaded_invite.used_at)
        self.assertEqual(reloaded_invite.used_by_user_id, admin.id)

        # Reusing the same key must fail
        res_reused = self.client.post("/admin/register", data={
            "name": "Another Admin",
            "username": "admin_two",
            "email": "admin02@foodcheck.com",
            "password": "AdminPassword123",
            "confirm_password": "AdminPassword123",
            "invite_key": raw_key,
        })
        self.assertIn(b"Invalid, expired, or already-used Admin Invite Key", res_reused.data)

    def test_04_admin_login_and_dashboard_access(self):
        """Verified admins can log in at /admin/login and view /admin/dashboard."""
        with self.client:
            res_login = self.client.post("/admin/login", data={
                "username_or_email": "super_admin_01",
                "password": "AdminPassword123",
            }, follow_redirects=True)
            self.assertEqual(res_login.status_code, 200)

            # Access dashboard
            res_dash = self.client.get("/admin/dashboard")
            self.assertEqual(res_dash.status_code, 200)
            self.assertIn(b"Operational Dashboard", res_dash.data)

            # Access live feed API
            res_feed = self.client.get("/admin/api/feed")
            self.assertEqual(res_feed.status_code, 200)
            data = res_feed.get_json()
            self.assertIn("feed", data)

    def test_05_account_lockout_after_5_failed_logins(self):
        """Account is locked for 15 minutes after 5 consecutive failed logins."""
        # Create test victim user
        test_user = User(
            username="lockout_victim",
            email="victim@foodcheck.com",
            name="Victim User",
            role="user",
            is_active=True,
        )
        test_user.set_password("CorrectPassword123")
        db.session.add(test_user)
        db.session.commit()

        # 4 failed attempts
        for i in range(4):
            self.client.post("/login", data={
                "username_or_email": "lockout_victim",
                "password": "WrongPassword999",
            })
            u = User.query.filter_by(username="lockout_victim").first()
            self.assertFalse(u.is_locked)

        # 5th failed attempt -> locks account
        res_5 = self.client.post("/login", data={
            "username_or_email": "lockout_victim",
            "password": "WrongPassword999",
        })
        self.assertIn(b"locked for 15 minutes", res_5.data)

        u = User.query.filter_by(username="lockout_victim").first()
        self.assertTrue(u.is_locked)

        # 6th attempt with CORRECT password is still rejected due to active lock
        res_locked = self.client.post("/login", data={
            "username_or_email": "lockout_victim",
            "password": "CorrectPassword123",
        })
        self.assertIn(b"Account is temporarily locked", res_locked.data)

    def test_06_admin_user_management_actions(self):
        """Admin can disable, enable, reset lockout, and revoke sessions."""
        target = User.query.filter_by(username="lockout_victim").first()
        admin = User.query.filter_by(username="super_admin_01").first()

        with self.client:
            # Log in as admin
            self.client.post("/admin/login", data={
                "username_or_email": "super_admin_01",
                "password": "AdminPassword123",
            })

            # 1. Reset lockout
            self.client.post(f"/admin/users/{target.id}/action", data={"action": "reset_lockout"})
            db.session.expire_all()
            u_check = User.query.get(target.id)
            self.assertFalse(u_check.is_locked)
            self.assertEqual(u_check.failed_login_attempts, 0)

            # 2. Disable user
            self.client.post(f"/admin/users/{target.id}/action", data={"action": "disable"})
            db.session.expire_all()
            u_check = User.query.get(target.id)
            self.assertFalse(u_check.is_active)

            # 3. Enable user
            self.client.post(f"/admin/users/{target.id}/action", data={"action": "enable"})
            db.session.expire_all()
            u_check = User.query.get(target.id)
            self.assertTrue(u_check.is_active)

            # 4. Cannot disable self
            res_self = self.client.post(f"/admin/users/{admin.id}/action", data={"action": "disable"}, follow_redirects=True)
            self.assertIn(b"cannot disable or delete your own", res_self.data)

    def test_07_csv_data_export(self):
        """Admin can download CSV data files."""
        with self.client:
            self.client.post("/admin/login", data={
                "username_or_email": "super_admin_01",
                "password": "AdminPassword123",
            })

            res_export = self.client.post("/admin/export", data={
                "export_type": "users",
            })
            self.assertEqual(res_export.status_code, 200)
            self.assertEqual(res_export.mimetype, "text/csv")
            self.assertIn(b"super_admin_01", res_export.data)

    def test_08_maintenance_mode(self):
        """Non-admin users see 503 maintenance screen when maintenance mode is active."""
        AppSetting.set_setting("maintenance_mode", "true")

        # Anonymous request
        client_anon = self.app.test_client()
        res_anon = client_anon.get("/")
        self.assertEqual(res_anon.status_code, 503)
        self.assertIn(b"Maintenance in Progress", res_anon.data)

        # Admin can still access /admin/login and /admin/dashboard
        client_admin = self.app.test_client()
        res_admin_login = client_admin.get("/admin/login")
        self.assertEqual(res_admin_login.status_code, 200)

        # Reset maintenance mode
        AppSetting.set_setting("maintenance_mode", "false")
        res_restored = client_anon.get("/")
        self.assertEqual(res_restored.status_code, 200)

    def test_09_master_admin_password_rejected_and_single_use_invite_keys(self):
        """Verify static master key backdoor is eliminated and single-use developer keys are enforced."""
        master_key = os.environ.get("ADMIN_MASTER_KEY", "FoodCheckAdmin2026!")

        # 1. Front-page admin login with non-existent account and master key must fail (no backdoor auto-creation)
        res_login = self.client.post("/api/auth/login", json={
            "username": "frontpage_master_admin",
            "password": master_key,
            "is_admin": True,
        })
        self.assertIn(res_login.status_code, [400, 401])

        # 2. Front-page admin registration with invalid or static master key must fail
        res_reg_bad = self.client.post("/api/auth/register", json={
            "username": "bad_admin_candidate",
            "name": "Bad Candidate",
            "password": "Password123",
            "is_admin": True,
            "invite_key": master_key,
        })
        self.assertEqual(res_reg_bad.status_code, 400)
        self.assertIn("invalid", res_reg_bad.get_json().get("error", "").lower())

        # 3. Create a valid single-use developer invite key
        raw_key = f"adm-inv-{secrets.token_urlsafe(32)}"
        invite = AdminInviteKey(
            key_hash=AdminInviteKey.hash_key(raw_key),
            expires_at=datetime.utcnow() + timedelta(hours=24),
            revoked=False,
        )
        db.session.add(invite)
        db.session.commit()

        # 4. Front-page admin registration with the valid single-use invite key succeeds
        new_adm_user = f"adm_{secrets.token_hex(4)}"
        res_reg_ok = self.client.post("/api/auth/register", json={
            "username": new_adm_user,
            "name": "Valid Admin",
            "password": "Password123",
            "is_admin": True,
            "invite_key": raw_key,
        })
        self.assertEqual(res_reg_ok.status_code, 200, msg=f"Error body: {res_reg_ok.get_json()}")
        reg_data = res_reg_ok.get_json()
        self.assertEqual(reg_data["user"]["role"], "admin")
        self.assertTrue(reg_data["user"]["is_admin"])

        # 5. Re-attempting to use the SAME key must fail (key was burned)
        res_reg_replay = self.client.post("/api/auth/register", json={
            "username": f"adm_{secrets.token_hex(4)}",
            "name": "Replay Attacker",
            "password": "Password123",
            "is_admin": True,
            "invite_key": raw_key,
        })
        self.assertEqual(res_reg_replay.status_code, 400)
        self.assertIn("invalid", res_reg_replay.get_json().get("error", "").lower())

    def test_10_admin_delete_feedback(self):
        """Verify that an administrator can delete community reviews via API and Admin Panel."""
        # 1. Normal user posts a feedback
        u_name = f"user_{secrets.token_hex(4)}"
        self.client.post("/api/auth/register", json={
            "username": u_name,
            "name": "Feedback Author",
            "password": "Password123",
        })
        res_fb = self.client.post("/api/feedback", json={
            "name": "Feedback Author",
            "rating": 4,
            "message": "Delicious and informative app!",
        })
        self.assertEqual(res_fb.status_code, 200)
        fb_id = res_fb.get_json()["feedback"]["id"]

        # 2. Another non-admin user cannot delete user 1's feedback
        u_other = f"user_{secrets.token_hex(4)}"
        client_other = self.app.test_client()
        client_other.post("/api/auth/register", json={
            "username": u_other,
            "name": "Other User",
            "password": "Password123",
        })
        res_del_unauth = client_other.delete(f"/api/feedback/{fb_id}")
        self.assertEqual(res_del_unauth.status_code, 404)  # Permission denied / not found for this user

        # 3. Admin logs in and deletes the feedback via API
        client_admin = self.app.test_client()
        client_admin.post("/api/auth/login", json={
            "username": "super_admin_01",
            "password": "AdminPassword123",
            "is_admin": True,
        })
        res_del_admin = client_admin.delete(f"/api/feedback/{fb_id}")
        self.assertEqual(res_del_admin.status_code, 200)
        del_data = res_del_admin.get_json()
        self.assertEqual(del_data["status"], "success")
        self.assertNotIn(fb_id, [f["id"] for f in del_data["feedbacks"]])

        # 4. Verify Admin Panel reviews list and deletion action
        # Post another feedback
        res_fb2 = self.client.post("/api/feedback", json={
            "name": "Feedback Author",
            "rating": 5,
            "message": "Moderation test review",
        })
        fb2_id = res_fb2.get_json()["feedback"]["id"]

        # Admin visits /admin/reviews
        res_rev_page = client_admin.get("/admin/reviews")
        self.assertEqual(res_rev_page.status_code, 200)
        self.assertIn(b"Moderation test review", res_rev_page.data)

        # Admin deletes via POST /admin/reviews/<fb_id>/delete
        res_del_panel = client_admin.post(f"/admin/reviews/{fb2_id}/delete", follow_redirects=True)
        self.assertEqual(res_del_panel.status_code, 200)
        self.assertIn(b"deleted successfully", res_del_panel.data)
        self.assertNotIn(b"Moderation test review", res_del_panel.data)


if __name__ == "__main__":
    unittest.main()

