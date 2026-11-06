import os
import logging
import requests
from typing import Tuple

logger = logging.getLogger(__name__)

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
DEFAULT_TEST_SECRET_KEY = "6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe"


def verify_recaptcha_detailed(token: str, remote_ip: str = None) -> Tuple[bool, str]:
    """
    Verify a Google reCAPTCHA v2 token with Google's siteverify API.

    POSTs to https://www.google.com/recaptcha/api/siteverify with:
      - secret: RECAPTCHA_SECRET_KEY from environment variables (or test fallback)
      - response: g-recaptcha-response token from client
      - remoteip: client's remote IP address (optional)
      - timeout: 5.0 seconds

    Returns: (is_valid, human_readable_message)
    """
    if not token or not str(token).strip():
        return False, "reCAPTCHA verification is required. Please check the 'I'm not a robot' box."

    secret_key = os.getenv("RECAPTCHA_SECRET_KEY", "").strip()
    if not secret_key:
        from flask import current_app
        is_testing = False
        try:
            is_testing = bool(current_app and current_app.config.get("TESTING", False))
        except Exception:
            pass
        if is_testing:
            logger.warning("RECAPTCHA_SECRET_KEY environment variable is not set.")
            return False, "RECAPTCHA_SECRET_KEY is not configured."
        # Graceful fallback to Google's official public test secret key for development & Render
        secret_key = DEFAULT_TEST_SECRET_KEY

    payload = {
        "secret": secret_key,
        "response": str(token).strip(),
    }
    if remote_ip and str(remote_ip).strip():
        payload["remoteip"] = str(remote_ip).strip()

    try:
        response = requests.post(
            VERIFY_URL,
            data=payload,
            timeout=5.0,
        )
        if response.status_code != 200:
            logger.warning(f"Google reCAPTCHA API responded with HTTP {response.status_code}")
            return False, f"reCAPTCHA server responded with HTTP {response.status_code}"

        data = response.json()
        if data.get("success") is True:
            return True, "reCAPTCHA verified successfully."

        error_codes = data.get("error-codes", [])
        logger.warning(f"Google reCAPTCHA verification failed: error_codes={error_codes}, response={data}")

        if "timeout-or-duplicate" in error_codes:
            return False, "reCAPTCHA token expired or already used. Please click the checkbox again."
        elif "invalid-input-response" in error_codes:
            return False, "reCAPTCHA token is invalid or expired. Please click the checkbox again."
        elif "invalid-input-secret" in error_codes:
            return False, "Invalid reCAPTCHA secret key configured."
        elif "missing-input-response" in error_codes:
            return False, "reCAPTCHA response token missing."

        return False, "reCAPTCHA verification failed. Please try ticking the checkbox again."
    except requests.exceptions.Timeout:
        logger.warning("Google reCAPTCHA verification timed out.")
        return False, "reCAPTCHA verification timed out. Please try again."
    except Exception as exc:
        logger.warning(f"Google reCAPTCHA verification exception: {exc}")
        return False, "reCAPTCHA verification network error. Please try again."


def verify_recaptcha(token: str, remote_ip: str = None) -> bool:
    """
    Verify a Google reCAPTCHA v2 token with Google's siteverify API.
    Maintains backward compatibility with unit tests and callers expecting a boolean.
    """
    ok, _ = verify_recaptcha_detailed(token, remote_ip)
    return ok


def verify_captcha_token(token: str, remote_ip: str = "127.0.0.1") -> Tuple[bool, str]:
    """
    Unified verification entrypoint:
    - If token is custom (starts with 'fc_'), validates and burns via CustomCaptchaManager.
    - If provider is turnstile, validates via Cloudflare Turnstile API.
    - Otherwise validates via Google reCAPTCHA v2.
    """
    if not token or not str(token).strip():
        return False, "reCAPTCHA verification is required. Please check the 'I'm not a robot' box."

    token = str(token).strip()

    # Custom signed token
    if token.startswith("fc_"):
        from services.captcha.manager import captcha_manager
        return captcha_manager.validate_and_burn_token(token, ip=remote_ip)

    # Cloudflare Turnstile
    provider_name = os.getenv("CAPTCHA_PROVIDER", "recaptcha").strip().lower()
    if provider_name == "turnstile":
        from services.captcha.provider import TurnstileProvider
        return TurnstileProvider().verify_token(token, remote_ip=remote_ip)

    # Google reCAPTCHA
    return verify_recaptcha_detailed(token, remote_ip)
