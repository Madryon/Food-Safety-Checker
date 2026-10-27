import os
import logging
import requests

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
