"""
FoodCheck CAPTCHA Package
Supports Custom 3x3/4x4 Image Grid + Audio CAPTCHA with provider abstraction (Custom, Turnstile, reCAPTCHA).
"""
from services.captcha.provider import get_captcha_provider, BaseCaptchaProvider
from services.captcha.manager import captcha_manager

__all__ = ["get_captcha_provider", "BaseCaptchaProvider", "captcha_manager"]
