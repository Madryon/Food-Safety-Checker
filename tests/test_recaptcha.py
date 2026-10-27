import os
import secrets
import unittest
from unittest.mock import patch, MagicMock
import requests

from app import app
from extensions import db, limiter
from models import User
from services.recaptcha import verify_recaptcha


class RecaptchaAndHoneypotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["RECAPTCHA_ENABLED"] = True
        app.config["RATELIMIT_ENABLED"] = False
        limiter.enabled = False

    def setUp(self):
        self.app = app
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        os.environ["RECAPTCHA_SITE_KEY"] = "test_recaptcha_site_key_123"
        os.environ["RECAPTCHA_SECRET_KEY"] = "test_recaptcha_secret_key_456"

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    # ── 1. HELPER UNIT TESTS: verify_recaptcha() ─────────────────────────────

    @patch("requests.post")
    def test_verify_recaptcha_helper_valid_token(self, mock_post):
        """Helper returns True when Google siteverify API returns success == True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "challenge_ts": "2026-10-27T12:00:00Z"}
        mock_post.return_value = mock_resp

        result = verify_recaptcha("valid_client_token", "192.168.1.100")
        self.assertTrue(result)

        mock_post.assert_called_once_with(
            "https://www.google.com/recaptcha/api/siteverify",
            data={
                "secret": "test_recaptcha_secret_key_456",
                "response": "valid_client_token",
                "remoteip": "192.168.1.100",
            },
            timeout=5.0,
        )

    def test_verify_recaptcha_helper_missing_token(self):
        """Helper returns False immediately when token is empty, None, or whitespace."""
        with patch("requests.post") as mock_post:
            self.assertFalse(verify_recaptcha(""))
            self.assertFalse(verify_recaptcha(None))
            self.assertFalse(verify_recaptcha("   "))
            mock_post.assert_not_called()

    def test_verify_recaptcha_helper_missing_secret(self):
        """Helper returns False when RECAPTCHA_SECRET_KEY is not configured."""
        with patch.dict(os.environ, {"RECAPTCHA_SECRET_KEY": ""}):
            with patch("requests.post") as mock_post:
                self.assertFalse(verify_recaptcha("some_token"))
                mock_post.assert_not_called()

    @patch("requests.post")
    def test_verify_recaptcha_helper_failed_verification(self, mock_post):
        """Helper returns False when Google siteverify API returns success == False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": False, "error-codes": ["invalid-input-response"]}
        mock_post.return_value = mock_resp

        result = verify_recaptcha("invalid_token", "127.0.0.1")
        self.assertFalse(result)

    @patch("requests.post")
    def test_verify_recaptcha_helper_network_error(self, mock_post):
        """Helper catches network exceptions (timeouts/connection errors) and returns False."""
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")
        result = verify_recaptcha("any_token", "127.0.0.1")
        self.assertFalse(result)

    @patch("requests.post")
    def test_verify_recaptcha_helper_http_error(self, mock_post):
        """Helper returns False when Google API returns non-200 HTTP status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        result = verify_recaptcha("any_token", "127.0.0.1")
        self.assertFalse(result)

    # ── 2. REGISTER ROUTE TESTS ──────────────────────────────────────────────

    def test_register_missing_recaptcha_returns_400(self):
        """Register without reCAPTCHA token is rejected with HTTP 400 and creates no DB user."""
        uniq = secrets.token_hex(4)
        username = f"bot_{uniq}"

        response = self.client.post("/register", data={
            "name": "Bot Attempt",
            "username": username,
            "password": "Password123",
            "confirm_password": "Password123",
            # No g-recaptcha-response provided
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"reCAPTCHA verification", response.data)

        # Verify user was NOT saved to the database
        created = User.query.filter_by(username=username).first()
        self.assertIsNone(created)

    @patch("services.recaptcha.requests.post")
    def test_register_failed_recaptcha_returns_400(self, mock_post):
        """Register with invalid reCAPTCHA token returns HTTP 400 and creates no DB user."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": False}
        mock_post.return_value = mock_resp

        uniq = secrets.token_hex(4)
        username = f"fakebot_{uniq}"

        response = self.client.post("/register", data={
            "name": "Fake Bot",
            "username": username,
            "password": "Password123",
            "confirm_password": "Password123",
            "g-recaptcha-response": "tampered_fake_token",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"reCAPTCHA verification", response.data)

        created = User.query.filter_by(username=username).first()
        self.assertIsNone(created)

    @patch("services.recaptcha.requests.post")
    def test_register_valid_recaptcha_succeeds(self, mock_post):
        """Register with valid reCAPTCHA token creates the user and redirects."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}
        mock_post.return_value = mock_resp

        uniq = secrets.token_hex(4)
        username = f"human_{uniq}"

        response = self.client.post("/register", data={
            "name": "Real Human",
            "username": username,
            "email": f"human_{uniq}@example.com",
            "password": "HumanPassword123",
            "confirm_password": "HumanPassword123",
            "g-recaptcha-response": "verified_token_abc",
        }, follow_redirects=False)

        self.assertEqual(response.status_code, 302)

        created = User.query.filter_by(username=username).first()
        self.assertIsNotNone(created)
        self.assertEqual(created.name, "Real Human")

    def test_register_honeypot_filled_returns_400(self):
        """Register with honeypot field filled is rejected with HTTP 400 and creates no DB user."""
        uniq = secrets.token_hex(4)
        username = f"spambot_{uniq}"

        response = self.client.post("/register", data={
            "name": "Spam Crawler",
            "username": username,
            "password": "Password123",
            "confirm_password": "Password123",
            "honeypot": "http://spam-link.ru",  # Honeypot filled by bot
            "g-recaptcha-response": "some_token",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Bot submission detected", response.data)

        created = User.query.filter_by(username=username).first()
        self.assertIsNone(created)

    # ── 3. LOGIN ROUTE TESTS ─────────────────────────────────────────────────

    def test_login_missing_recaptcha_returns_400(self):
        """Login without reCAPTCHA token returns HTTP 400."""
        response = self.client.post("/login", data={
            "username_or_email": "anyone",
            "password": "Password123",
            # No g-recaptcha-response
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"reCAPTCHA verification", response.data)

    @patch("services.recaptcha.requests.post")
    def test_login_valid_recaptcha_checks_credentials(self, mock_post):
        """Login with valid reCAPTCHA token proceeds to authentication."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}
        mock_post.return_value = mock_resp

        # Create user
        uniq = secrets.token_hex(4)
        user = User(
            username=f"login_tester_{uniq}",
            name="Login Tester",
            role="user",
            is_active=True,
        )
        user.set_password("CorrectPassword123")
        db.session.add(user)
        db.session.commit()

        response = self.client.post("/login", data={
            "username_or_email": user.username,
            "password": "CorrectPassword123",
            "g-recaptcha-response": "valid_login_token",
        }, follow_redirects=False)

        self.assertEqual(response.status_code, 302)

    # ── 4. RATE LIMITING TEST ────────────────────────────────────────────────

    def test_rate_limiting_enforcement(self):
        """Rate limiter blocks rapid repeated attempts (5 per minute limit)."""
        app.config["RATELIMIT_ENABLED"] = True
        limiter.enabled = True

        hit_429 = False
        # Fire 8 consecutive POST requests to /register
        for i in range(8):
            res = self.client.post("/register", data={
                "name": f"Spam {i}",
                "username": f"rapid_user_{i}",
                "password": "Password123",
                "confirm_password": "Password123",
            })
            if res.status_code == 429:
                hit_429 = True
                break

        # Restore limiter state for other tests
        app.config["RATELIMIT_ENABLED"] = False
        limiter.enabled = False

        self.assertTrue(hit_429, "Expected rate limit (429) to be triggered within 8 requests.")


if __name__ == "__main__":
    unittest.main()
