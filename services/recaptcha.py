import os
import logging
import requests
from typing import Tuple

logger = logging.getLogger(__name__)

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


def verify_recaptcha(token: str, remote_ip: str = None) -> bool:
    """
    Verify a Google reCAPTCHA v2 token with Google's siteverify API.

    POSTs to https://www.google.com/recaptcha/api/siteverify with:
      - secret: RECAPTCHA_SECRET_KEY from environment variables
      - response: g-recaptcha-response token from client
      - remoteip: client's remote IP address (optional)
      - timeout: 5.0 seconds

    Returns False if:
      - token is missing, None, or empty string
      - RECAPTCHA_SECRET_KEY is missing or empty
      - network error, timeout, or non-200 HTTP response
      - API returns success != True
    Returns True only if Google verifies success == True.
    """
    if not token or not str(token).strip():
        return False

    secret_key = os.getenv("RECAPTCHA_SECRET_KEY", "").strip()
    if not secret_key:
        logger.warning("RECAPTCHA_SECRET_KEY environment variable is not set.")
        return False

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
            return False

        data = response.json()
        return bool(data.get("success") is True)
    except Exception as exc:
        logger.warning(f"Google reCAPTCHA verification exception: {exc}")
        return False


def verify_captcha_token(token: str, remote_ip: str = "127.0.0.1") -> Tuple[bool, str]:
    """
    Unified verification entrypoint:
    - If token is custom (starts with 'fc_'), validates and burns via CustomCaptchaManager.
    - If provider is turnstile, validates via Cloudflare Turnstile API.
    - Otherwise validates via Google reCAPTCHA v2.
    """
    if not token or not str(token).strip():
        return False, "CAPTCHA verification is required."

    token = str(token).strip()

    # Custom signed token
    if token.startswith("fc_"):
        from services.captcha.manager import captcha_manager
        return captcha_manager.validate_and_burn_token(token, ip=remote_ip)

    # Cloudflare Turnstile
    provider_name = os.getenv("CAPTCHA_PROVIDER", "custom").strip().lower()
    if provider_name == "turnstile":
        from services.captcha.provider import TurnstileProvider
        return TurnstileProvider().verify_token(token, remote_ip=remote_ip)

    # Google reCAPTCHA
    is_valid = verify_recaptcha(token, remote_ip)
    if is_valid:
        return True, "reCAPTCHA verified successfully."
    return False, "reCAPTCHA verification failed."
