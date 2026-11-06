"""
services/captcha/provider.py
Captcha Provider Interface and Implementations:
- CustomCaptchaProvider: Self-hosted 3x3/4x4 Image Grid + Audio CAPTCHA
- RecaptchaProvider: Google reCAPTCHA v2 Checkbox
- TurnstileProvider: Cloudflare Turnstile Checkbox
"""

import os
import requests
from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple
from services.captcha.manager import captcha_manager

class BaseCaptchaProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name identifier of the provider."""
        pass

    @abstractmethod
    def get_client_config(self) -> Dict[str, Any]:
        """Returns public client configuration and site keys."""
        pass

    @abstractmethod
    def verify_token(self, token: str, remote_ip: str = "127.0.0.1") -> Tuple[bool, str]:
        """
        Verifies the token from the client.
        Returns: (is_valid, message)
        """
        pass


class CustomCaptchaProvider(BaseCaptchaProvider):
    @property
    def provider_name(self) -> str:
        return "custom"

    def get_client_config(self) -> Dict[str, Any]:
        return {
            "provider": "custom",
            "siteKey": None,
            "challengeEndpoint": "/api/captcha/challenge",
            "verifyEndpoint": "/api/captcha/verify",
        }

    def verify_token(self, token: str, remote_ip: str = "127.0.0.1") -> Tuple[bool, str]:
        if not token:
            return False, "Custom CAPTCHA token missing."
        return captcha_manager.validate_and_burn_token(token, ip=remote_ip)


class RecaptchaProvider(BaseCaptchaProvider):
    VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
    DEFAULT_SITE_KEY = "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
    DEFAULT_SECRET_KEY = "6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe"

    @property
    def provider_name(self) -> str:
        return "recaptcha"

    def get_client_config(self) -> Dict[str, Any]:
        site_key = os.getenv("RECAPTCHA_SITE_KEY", "").strip() or self.DEFAULT_SITE_KEY
        return {
            "provider": "recaptcha",
            "siteKey": site_key,
        }

    def verify_token(self, token: str, remote_ip: str = "127.0.0.1") -> Tuple[bool, str]:
        from services.recaptcha import verify_recaptcha
        if not token or not str(token).strip():
            return False, "reCAPTCHA response token missing."
        if verify_recaptcha(token, remote_ip):
            return True, "reCAPTCHA verified successfully."
        return False, "reCAPTCHA verification failed."


class TurnstileProvider(BaseCaptchaProvider):
    VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    @property
    def provider_name(self) -> str:
        return "turnstile"

    def get_client_config(self) -> Dict[str, Any]:
        return {
            "provider": "turnstile",
            "siteKey": os.getenv("TURNSTILE_SITE_KEY", ""),
        }

    def verify_token(self, token: str, remote_ip: str = "127.0.0.1") -> Tuple[bool, str]:
        secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
        if not secret:
            return False, "TURNSTILE_SECRET_KEY is not configured."
        if not token:
            return False, "Turnstile response token missing."

        payload = {"secret": secret, "response": token}
        if remote_ip and remote_ip not in ("127.0.0.1", "::1", "localhost"):
            payload["remoteip"] = remote_ip

        try:
            resp = requests.post(self.VERIFY_URL, data=payload, timeout=5)
            if resp.status_code != 200:
                return False, f"Turnstile verification returned HTTP {resp.status_code}"
            result = resp.json()
            if result.get("success") is True:
                return True, "Turnstile verified successfully."
            return False, "Turnstile verification failed."
        except Exception as e:
            return False, f"Turnstile verification network error: {e}"


_PROVIDERS = {
    "custom": CustomCaptchaProvider(),
    "recaptcha": RecaptchaProvider(),
    "turnstile": TurnstileProvider(),
}

def get_captcha_provider() -> BaseCaptchaProvider:
    """Returns the configured CAPTCHA provider from environment (defaults to custom visual challenge)."""
    provider_name = os.getenv("CAPTCHA_PROVIDER", "custom").strip().lower()
    return _PROVIDERS.get(provider_name, _PROVIDERS["custom"])
