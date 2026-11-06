"""
services/captcha/manager.py
Server-side Custom CAPTCHA state manager:
- One-time use challenges with 2-minute TTL.
- One-time use signed tokens with 5-minute TTL.
- Server-only stored answers (zero client hints).
- Rate limits: 10 challenges/min, 5 failed verifications in 10 min -> 5 min cooldown.
- Minimum 2-second time-to-submit check.
- Adaptive grid difficulty: 3x3 default, 4x4 if failed logins >= 3.
"""

import os
import time
import hmac
import hashlib
import secrets
import threading
from typing import Dict, Any, Optional, Set, Tuple, List
from captcha_config import (
    IMAGE_BANK_DIR,
    CATEGORIES,
    NEGATIVES_CATEGORY,
    GRID_SIZE_DEFAULT,
    GRID_SIZE_HARD,
    MIN_CORRECT_DEFAULT,
    MAX_CORRECT_DEFAULT,
    MIN_CORRECT_HARD,
    MAX_CORRECT_HARD,
    CHALLENGE_TTL_SECONDS,
    TOKEN_TTL_SECONDS,
    MIN_TIME_TO_SUBMIT_SECONDS,
    RATE_LIMIT_CHALLENGES_PER_MIN,
    MAX_FAILED_VERIFICATIONS,
    FAILED_VERIFICATION_WINDOW,
    COOLDOWN_SECONDS,
    FAILED_LOGINS_FOR_HARD_GRID,
)
from services.captcha.image_processor import process_captcha_image
from services.captcha.audio import generate_captcha_audio_wav

class CustomCaptchaManager:
    def __init__(self):
        self._lock = threading.Lock()
        # In-memory storage with thread-safe locks
        self.challenges: Dict[str, Dict[str, Any]] = {}
        self.tokens: Dict[str, Dict[str, Any]] = {}
        # Rate limiting structures: {ip: [timestamps]}
        self.challenge_requests: Dict[str, List[float]] = {}
        self.failed_verifications: Dict[str, List[float]] = {}
        self.cooldown_until: Dict[str, float] = {}
        # Failed logins tracker: {ip: count}
        self.failed_logins: Dict[str, int] = {}
        # Secret key for token signatures
        self.secret_key = os.getenv("SECRET_KEY", "foodcheck-captcha-secret-key-salt-98745")

    def _cleanup_expired(self):
        """Prunes expired challenges and tokens to keep memory footprint minimal."""
        now = time.time()
        # Clean challenges
        expired_ch = [cid for cid, ch in self.challenges.items() if now - ch["created_at"] > CHALLENGE_TTL_SECONDS * 2]
        for cid in expired_ch:
            self.challenges.pop(cid, None)

        # Clean tokens
        expired_tok = [tid for tid, tok in self.tokens.items() if now > tok["expires_at"] + 60]
        for tid in expired_tok:
            self.tokens.pop(tid, None)

    def _check_cooldown(self, ip: str) -> Optional[int]:
        """Returns remaining cooldown seconds if IP is blocked, or None."""
        now = time.time()
        cd = self.cooldown_until.get(ip, 0)
        if cd > now:
            return int(cd - now) + 1
        return None

    def _record_challenge_request(self, ip: str) -> bool:
        """Returns True if within rate limit (10/min), False if exceeded."""
        now = time.time()
        history = self.challenge_requests.setdefault(ip, [])
        # Keep only timestamps in last 60 seconds
        history = [t for t in history if now - t < 60.0]
        if len(history) >= RATE_LIMIT_CHALLENGES_PER_MIN:
            self.challenge_requests[ip] = history
            return False
        history.append(now)
        self.challenge_requests[ip] = history
        return True

    def _record_failed_verification(self, ip: str) -> bool:
        """Records a failed verification. Returns True if cooldown triggered."""
        now = time.time()
        history = self.failed_verifications.setdefault(ip, [])
        # Keep only in window
        history = [t for t in history if now - t < FAILED_VERIFICATION_WINDOW]
        history.append(now)
        self.failed_verifications[ip] = history

        if len(history) >= MAX_FAILED_VERIFICATIONS:
            self.cooldown_until[ip] = now + COOLDOWN_SECONDS
            self.failed_verifications.pop(ip, None)
            return True
        return False

    def record_failed_login(self, ip: str):
        """Increments failed logins for IP to dynamically scale difficulty."""
        with self._lock:
            self.failed_logins[ip] = self.failed_logins.get(ip, 0) + 1

    def record_successful_login(self, ip: str):
        """Resets failed logins counter on successful login."""
        with self._lock:
            self.failed_logins.pop(ip, None)

    def create_challenge(self, ip: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
        """
        Creates a new CAPTCHA challenge.
        Returns: (challenge_dict, error_message, status_code)
        """
        with self._lock:
            self._cleanup_expired()

            # 1. Cooldown check
            cd = self._check_cooldown(ip)
            if cd:
                return None, f"Too many failed verification attempts (cooldown active). Please wait {cd} seconds.", 429

            # 2. Rate limit (10 challenges/min per IP)
            if not self._record_challenge_request(ip):
                return None, "Challenge rate limit exceeded (maximum 10 per minute). Please try again shortly.", 429

            # 3. Determine grid size based on failed login attempts
            failed_count = self.failed_logins.get(ip, 0)
            if failed_count >= FAILED_LOGINS_FOR_HARD_GRID:
                grid_size = GRID_SIZE_HARD  # 4x4
                total_slots = grid_size * grid_size  # 16
                min_c, max_c = MIN_CORRECT_HARD, MAX_CORRECT_HARD
            else:
                grid_size = GRID_SIZE_DEFAULT  # 3x3
                total_slots = grid_size * grid_size  # 9
                min_c, max_c = MIN_CORRECT_DEFAULT, MAX_CORRECT_DEFAULT

            # 4. Pick target category
            target_key = secrets.choice(list(CATEGORIES.keys()))
            target_info = CATEGORIES[target_key]

            # 5. Gather images
            target_dir = os.path.join(IMAGE_BANK_DIR, target_key)
            target_files = [
                os.path.join(target_dir, f)
                for f in os.listdir(target_dir)
                if f.lower().endswith((".jpg", ".jpeg", ".png"))
            ] if os.path.exists(target_dir) else []

            if not target_files:
                return None, "Image bank not initialized. Please run seed script.", 500

            num_correct = secrets.randbelow(max_c - min_c + 1) + min_c
            num_correct = min(num_correct, len(target_files))
            selected_target_files = secrets.SystemRandom().sample(target_files, num_correct)

            # Distractors from other categories and negatives
            distractor_files = []
            other_cats = [c for c in CATEGORIES.keys() if c != target_key] + [NEGATIVES_CATEGORY]
            for c in other_cats:
                c_dir = os.path.join(IMAGE_BANK_DIR, c)
                if os.path.exists(c_dir):
                    distractor_files.extend([
                        os.path.join(c_dir, f)
                        for f in os.listdir(c_dir)
                        if f.lower().endswith((".jpg", ".jpeg", ".png"))
                    ])

            needed_distractors = total_slots - num_correct
            if len(distractor_files) < needed_distractors:
                # Duplicate or reuse if bank small
                distractor_files = distractor_files * (needed_distractors // max(1, len(distractor_files)) + 1)
            selected_distractors = secrets.SystemRandom().sample(distractor_files, needed_distractors)

            # Combine and shuffle
            slots = []
            for f in selected_target_files:
                slots.append({"is_target": True, "path": f})
            for f in selected_distractors:
                slots.append({"is_target": False, "path": f})

            secrets.SystemRandom().shuffle(slots)

            # Store correct indexes (0-indexed)
            correct_indexes = {i for i, s in enumerate(slots) if s["is_target"]}

            # 6. Generate 4-digit code for audio alternative
            audio_digits = "".join(str(secrets.randbelow(10)) for _ in range(4))

            # 7. Create Challenge Record
            challenge_id = secrets.token_urlsafe(24)
            self.challenges[challenge_id] = {
                "challenge_id": challenge_id,
                "category": target_key,
                "correct_indexes": correct_indexes,
                "audio_digits": audio_digits,
                "grid_size": grid_size,
                "image_paths": [s["path"] for s in slots],
                "created_at": time.time(),
                "ip": ip,
                "used": False,
            }

            # 8. Return client payload — NO answers, NO categories in filenames
            image_urls = [f"/api/captcha/image/{challenge_id}/{idx}" for idx in range(total_slots)]

            return {
                "challengeId": challenge_id,
                "instruction": target_info["instruction"],
                "label": target_info["label"],
                "gridSize": grid_size,
                "images": image_urls,
                "hasAudio": True,
            }, None, 200

    def get_image_bytes(self, challenge_id: str, img_idx: int) -> Optional[bytes]:
        """Retrieves and processes the JPEG image for a challenge slot."""
        with self._lock:
            challenge = self.challenges.get(challenge_id)
            if not challenge:
                return None
            if img_idx < 0 or img_idx >= len(challenge["image_paths"]):
                return None
            image_path = challenge["image_paths"][img_idx]

        return process_captcha_image(image_path)

    def get_audio_bytes(self, challenge_id: str) -> Optional[bytes]:
        """Retrieves WAV audio bytes for a challenge."""
        with self._lock:
            challenge = self.challenges.get(challenge_id)
            if not challenge:
                return None
            digits = challenge["audio_digits"]

        return generate_captcha_audio_wav(digits)

    def verify_challenge(
        self,
        challenge_id: str,
        selected_indexes: Optional[List[int]] = None,
        audio_answer: Optional[str] = None,
        ip: str = "127.0.0.1"
    ) -> Tuple[bool, Optional[str], Optional[str], int]:
        """
        Verifies challenge answers.
        Challenge is strictly ONE-TIME USE.
        Returns: (success, token_or_none, error_message_or_none, http_status)
        """
        with self._lock:
            # 1. Cooldown check
            cd = self._check_cooldown(ip)
            if cd:
                return False, None, f"Too many failed verification attempts (cooldown active). Please wait {cd} seconds.", 429

            # 2. Challenge retrieval and burn (ONE-TIME USE)
            challenge = self.challenges.pop(challenge_id, None)
            if not challenge or challenge.get("used"):
                self._record_failed_verification(ip)
                return False, None, "Challenge expired or invalid. Please try again.", 400

            now = time.time()
            created_at = challenge["created_at"]

            # 3. Check TTL (2 minutes)
            if now - created_at > CHALLENGE_TTL_SECONDS:
                self._record_failed_verification(ip)
                return False, None, "Challenge expired. Please click refresh for a new one.", 400

            # 4. Minimum 2-second time-to-submit check
            if now - created_at < MIN_TIME_TO_SUBMIT_SECONDS:
                self._record_failed_verification(ip)
                return False, None, "Submission too fast. Automated bot behavior detected.", 400

            # 5. Check correctness
            is_valid = False
            if audio_answer is not None and str(audio_answer).strip():
                clean_ans = str(audio_answer).strip()
                is_valid = (clean_ans == challenge["audio_digits"])
            elif selected_indexes is not None:
                try:
                    user_selected = {int(x) for x in selected_indexes}
                except (ValueError, TypeError):
                    user_selected = set()
                is_valid = (user_selected == challenge["correct_indexes"])

            if not is_valid:
                locked = self._record_failed_verification(ip)
                if locked:
                    return False, None, "Too many failed attempts. Cooldown of 5 minutes activated.", 429
                return False, None, "Incorrect selection. Please try again.", 400

            # 6. Success! Reset failure count and issue signed token
            self.failed_verifications.pop(ip, None)
            token = self._issue_signed_token(ip)
            return True, token, None, 200

    def _issue_signed_token(self, ip: str) -> str:
        """Issues an HMAC-signed one-time token valid for 5 minutes."""
        token_id = secrets.token_urlsafe(24)
        expires_at = int(time.time() + TOKEN_TTL_SECONDS)

        # Signature: HMAC-SHA256(secret_key, token_id:ip:expires_at)
        raw_sig_data = f"{token_id}:{ip}:{expires_at}".encode("utf-8")
        sig = hmac.new(self.secret_key.encode("utf-8"), raw_sig_data, hashlib.sha256).hexdigest()

        token_str = f"fc_{token_id}.{expires_at}.{sig}"
        self.tokens[token_id] = {
            "token_id": token_id,
            "ip": ip,
            "expires_at": expires_at,
            "used": False,
        }
        return token_str

    def validate_and_burn_token(self, token: str, ip: str = None) -> Tuple[bool, str]:
        """
        Validates the token signature, expiry, and IP binding, then burns it immediately.
        Returns: (is_valid, reason)
        """
        if not token or not isinstance(token, str):
            return False, "Missing or invalid CAPTCHA token."

        parts = token.split(".")
        if len(parts) != 3 or not parts[0].startswith("fc_"):
            return False, "Malformed CAPTCHA token format."

        token_id = parts[0][3:]
        try:
            expires_at = int(parts[1])
        except ValueError:
            return False, "Invalid token timestamp."
        received_sig = parts[2]

        now = time.time()
        if now > expires_at:
            return False, "CAPTCHA token has expired (5 minute validity limit)."

        with self._lock:
            tok_record = self.tokens.get(token_id)
            if not tok_record:
                return False, "Unrecognized or previously purged CAPTCHA token."

            if now > tok_record.get("expires_at", 0):
                return False, "CAPTCHA token has expired (5 minute validity limit)."

            if tok_record["used"]:
                return False, "CAPTCHA token has already been used (replay rejected)."

            bound_ip = tok_record["ip"]
            # Validate signature
            raw_sig_data = f"{token_id}:{bound_ip}:{expires_at}".encode("utf-8")
            expected_sig = hmac.new(self.secret_key.encode("utf-8"), raw_sig_data, hashlib.sha256).hexdigest()

            if not hmac.compare_digest(received_sig, expected_sig):
                return False, "Invalid CAPTCHA token signature."

            # Verify IP if provided and not localhost/internal
            if ip and bound_ip and bound_ip not in ("127.0.0.1", "::1", "localhost") and ip not in ("127.0.0.1", "::1", "localhost"):
                if ip != bound_ip:
                    return False, "CAPTCHA token IP binding mismatch."

            # Burn the token!
            tok_record["used"] = True

        return True, "Token successfully validated and consumed."

captcha_manager = CustomCaptchaManager()
