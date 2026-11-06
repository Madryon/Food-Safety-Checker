"""
tests/test_custom_captcha.py
Comprehensive test suite for the Custom 3x3/4x4 Image Grid + Audio CAPTCHA system:
1. Challenge generation (randomized grid, no leaked answers/categories)
2. Image and audio endpoint serving
3. Minimum 2-second time check rejection
4. Wrong answer failure
5. Correct answer passes and returns signed token
6. Audio alternative verification
7. Token validation and replay rejection (burned after single use)
8. Expired token rejection
9. Rate limiting on challenge requests (10/min)
10. 5 failed verifications triggers cooldown (429)
11. Honeypot rejection
12. Auth endpoints require valid token (burned on use)
13. Adaptive difficulty (3 failed logins -> 4x4 grid)
14. Provider switch (custom, recaptcha, turnstile)
"""

import os
import time
import json
import unittest
from unittest.mock import patch
from app import app
from extensions import db, limiter
from services.captcha import get_captcha_provider, captcha_manager
from services.captcha.provider import CustomCaptchaProvider, RecaptchaProvider, TurnstileProvider
from services.recaptcha import verify_captcha_token
from models import User

class CustomCaptchaTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["RATELIMIT_ENABLED"] = False
        app.config["WTF_CSRF_ENABLED"] = False
        limiter.enabled = False

    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()
        # Reset captcha manager state for test isolation
        with captcha_manager._lock:
            captcha_manager.challenges.clear()
            captcha_manager.tokens.clear()
            captcha_manager.challenge_requests.clear()
            captcha_manager.failed_verifications.clear()
            captcha_manager.cooldown_until.clear()
            captcha_manager.failed_logins.clear()

    def tearDown(self):
        app.config.pop("CAPTCHA_ENABLED", None)
        self.app_context.pop()

    def test_01_challenge_generation(self):
        """GET /api/captcha/challenge returns randomized grid without exposing answers or categories."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.50"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        self.assertTrue(data.get("success"))
        self.assertIn("challengeId", data)
        self.assertIn("instruction", data)
        self.assertEqual(data.get("gridSize"), 3)
        self.assertEqual(len(data.get("images", [])), 9)
        self.assertTrue(data.get("hasAudio"))

        # Verify that URLs do not leak category names or internal filenames
        for url in data["images"]:
            self.assertTrue(url.startswith("/api/captcha/image/"))
            for cat in ["traffic_lights", "bicycles", "cars", "fire_hydrants", "bridges", "buses", "motorcycles", "crosswalks"]:
                self.assertNotIn(cat, url)

    def test_02_image_and_audio_endpoints(self):
        """Image and audio endpoints return valid binary JPEG and WAV responses with no-cache headers."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.51"})
        data = res.get_json()
        cid = data["challengeId"]

        # Fetch image 0
        img_res = self.client.get(f"/api/captcha/image/{cid}/0")
        self.assertEqual(img_res.status_code, 200)
        self.assertEqual(img_res.content_type, "image/jpeg")
        self.assertGreater(len(img_res.data), 1000)
        self.assertIn("no-store", img_res.headers.get("Cache-Control", ""))

        # Invalid index returns 404
        bad_img = self.client.get(f"/api/captcha/image/{cid}/99")
        self.assertEqual(bad_img.status_code, 404)

        # Fetch audio
        aud_res = self.client.get(f"/api/captcha/audio/{cid}")
        self.assertEqual(aud_res.status_code, 200)
        self.assertEqual(aud_res.content_type, "audio/wav")
        self.assertGreater(len(aud_res.data), 1000)

    def test_03_min_time_rejection(self):
        """Submissions submitted faster than 2.0 seconds are rejected as bot activity."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.52"})
        cid = res.get_json()["challengeId"]

        # Immediate verification attempt
        v_res = self.client.post("/api/captcha/verify", json={
            "challengeId": cid,
            "selectedIndexes": [0, 1]
        }, environ_base={"REMOTE_ADDR": "192.168.1.52"})

        self.assertEqual(v_res.status_code, 400)
        data = v_res.get_json()
        self.assertFalse(data.get("success"))
        self.assertIn("too fast", data.get("message", "").lower())

    def test_04_wrong_answer_rejection(self):
        """Incorrect grid selections are rejected."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.53"})
        cid = res.get_json()["challengeId"]

        # Artificially age the challenge past 2.0s to bypass min time check
        with captcha_manager._lock:
            captcha_manager.challenges[cid]["created_at"] -= 3.0

        v_res = self.client.post("/api/captcha/verify", json={
            "challengeId": cid,
            "selectedIndexes": [999]  # Wrong index
        }, environ_base={"REMOTE_ADDR": "192.168.1.53"})

        self.assertEqual(v_res.status_code, 400)
        data = v_res.get_json()
        self.assertFalse(data.get("success"))
        self.assertIn("incorrect", data.get("message", "").lower())

    def test_05_correct_answer_returns_token(self):
        """Providing the exact correct indexes returns a signed CAPTCHA token."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.54"})
        cid = res.get_json()["challengeId"]

        with captcha_manager._lock:
            correct = list(captcha_manager.challenges[cid]["correct_indexes"])
            captcha_manager.challenges[cid]["created_at"] -= 3.0

        v_res = self.client.post("/api/captcha/verify", json={
            "challengeId": cid,
            "selectedIndexes": correct
        }, environ_base={"REMOTE_ADDR": "192.168.1.54"})

        self.assertEqual(v_res.status_code, 200)
        data = v_res.get_json()
        self.assertTrue(data.get("success"))
        token = data.get("token")
        self.assertTrue(token.startswith("fc_"))

    def test_06_audio_alternative_verification(self):
        """Providing the correct 4 audio digits verifies the challenge."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.55"})
        cid = res.get_json()["challengeId"]

        with captcha_manager._lock:
            digits = captcha_manager.challenges[cid]["audio_digits"]
            captcha_manager.challenges[cid]["created_at"] -= 3.0

        v_res = self.client.post("/api/captcha/verify", json={
            "challengeId": cid,
            "audioAnswer": digits
        }, environ_base={"REMOTE_ADDR": "192.168.1.55"})

        self.assertEqual(v_res.status_code, 200)
        data = v_res.get_json()
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("token").startswith("fc_"))

    def test_07_one_time_use_token_burn_and_replay_rejection(self):
        """A verified token can only be consumed once; subsequent replay attempts are rejected."""
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": "192.168.1.56"})
        cid = res.get_json()["challengeId"]

        with captcha_manager._lock:
            correct = list(captcha_manager.challenges[cid]["correct_indexes"])
            captcha_manager.challenges[cid]["created_at"] -= 3.0

        v_res = self.client.post("/api/captcha/verify", json={
            "challengeId": cid,
            "selectedIndexes": correct
        }, environ_base={"REMOTE_ADDR": "192.168.1.56"})
        token = v_res.get_json()["token"]

        # First burn succeeds
        ok1, msg1 = captcha_manager.validate_and_burn_token(token, ip="192.168.1.56")
        self.assertTrue(ok1)

        # Second use (replay attack) fails
        ok2, msg2 = captcha_manager.validate_and_burn_token(token, ip="192.168.1.56")
        self.assertFalse(ok2)
        self.assertIn("already been used", msg2.lower())

    def test_08_expired_token_rejection(self):
        """Tokens past their 5-minute TTL are rejected."""
        token = captcha_manager._issue_signed_token(ip="192.168.1.57")
        token_id = token.split(".")[0][3:]

        # Artificially expire token
        with captcha_manager._lock:
            captcha_manager.tokens[token_id]["expires_at"] = time.time() - 10

        # Attempt verification
        ok, msg = captcha_manager.validate_and_burn_token(token, ip="192.168.1.57")
        self.assertFalse(ok)
        self.assertIn("expired", msg.lower())

    def test_09_rate_limit_challenges(self):
        """Requesting more than 10 challenges per minute triggers rate limiting (HTTP 429)."""
        ip = "192.168.1.58"
        for i in range(10):
            res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": ip})
            self.assertEqual(res.status_code, 200)

        # 11th request triggers rate limit
        res_limit = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": ip})
        self.assertEqual(res_limit.status_code, 429)

    def test_10_rate_limit_cooldown_on_failed_verifications(self):
        """5 failed verifications triggers a 5-minute cooldown period (HTTP 429)."""
        ip = "192.168.1.59"
        for i in range(5):
            res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": ip})
            cid = res.get_json()["challengeId"]
            with captcha_manager._lock:
                captcha_manager.challenges[cid]["created_at"] -= 3.0
            # Wrong answer
            self.client.post("/api/captcha/verify", json={
                "challengeId": cid,
                "selectedIndexes": [999]
            }, environ_base={"REMOTE_ADDR": ip})

        # Next challenge request is blocked by cooldown
        res_blocked = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": ip})
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn("cooldown", res_blocked.get_json().get("message", "").lower())

    def test_11_honeypot_rejection(self):
        """Auth endpoints reject submissions where the honeypot field is filled."""
        app.config["CAPTCHA_ENABLED"] = False
        res = self.client.post("/api/auth/register", json={
            "username": "botuser",
            "password": "botpassword123",
            "honeypot": "I am a bot"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("Bot", res.get_json().get("error", ""))

    def test_12_auth_endpoints_require_valid_token(self):
        """Signup/login endpoints reject requests without a valid CAPTCHA token when enabled."""
        app.config["CAPTCHA_ENABLED"] = True
        test_user = f"humantester_{int(time.time() * 1000) % 100000}"

        # 1. Missing token -> 400
        res_missing = self.client.post("/api/auth/register", json={
            "username": f"{test_user}_missing",
            "password": "humanpassword123",
            "name": "Human Tester",
        }, environ_base={"REMOTE_ADDR": "192.168.1.60"})
        self.assertEqual(res_missing.status_code, 400)

        # 2. Issue valid token
        token = captcha_manager._issue_signed_token(ip="192.168.1.60")

        # 3. Submit with valid token -> 200
        res_ok = self.client.post("/api/auth/register", json={
            "username": test_user,
            "password": "humanpassword123",
            "name": "Human Tester",
            "captcha_token": token
        }, environ_base={"REMOTE_ADDR": "192.168.1.60"})
        self.assertEqual(res_ok.status_code, 200)

        # 4. Token was burned, so re-using it fails
        res_reused = self.client.post("/api/auth/login", json={
            "username": test_user,
            "password": "humanpassword123",
            "captcha_token": token
        }, environ_base={"REMOTE_ADDR": "192.168.1.60"})
        self.assertEqual(res_reused.status_code, 400)

        # Reset flag
        app.config["CAPTCHA_ENABLED"] = False

    def test_13_adaptive_difficulty_after_failed_logins(self):
        """After 3 failed logins, the IP receives a harder challenge (4x4 grid with 16 images)."""
        ip = "192.168.1.61"

        # Record 3 failed logins
        for _ in range(3):
            captcha_manager.record_failed_login(ip)

        # Challenge should now be 4x4
        res = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": ip})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("gridSize"), 4)
        self.assertEqual(len(data.get("images", [])), 16)

        # After successful login, it returns to 3x3
        captcha_manager.record_successful_login(ip)
        res_reset = self.client.get("/api/captcha/challenge", environ_base={"REMOTE_ADDR": ip})
        self.assertEqual(res_reset.status_code, 200)
        self.assertEqual(res_reset.get_json().get("gridSize"), 3)

    def test_14_provider_switch(self):
        """Provider switch selects custom, recaptcha, or turnstile based on CAPTCHA_PROVIDER."""
        with patch.dict(os.environ, {"CAPTCHA_PROVIDER": "custom"}):
            self.assertEqual(get_captcha_provider().provider_name, "custom")

        with patch.dict(os.environ, {"CAPTCHA_PROVIDER": "recaptcha"}):
            self.assertEqual(get_captcha_provider().provider_name, "recaptcha")

        with patch.dict(os.environ, {"CAPTCHA_PROVIDER": "turnstile"}):
            self.assertEqual(get_captcha_provider().provider_name, "turnstile")


if __name__ == "__main__":
    unittest.main()
