import os

# Base directory for storing seed image bank
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_BANK_DIR = os.path.join(BASE_DIR, "data", "captcha_images")

# Active provider: 'custom' (default), 'recaptcha', or 'turnstile'
DEFAULT_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "custom").lower()

# Grid Dimensions
GRID_SIZE_DEFAULT = 3  # 3x3 = 9 images
GRID_SIZE_HARD = 4     # 4x4 = 16 images

# Number of correct images to pick
MIN_CORRECT_DEFAULT = 2
MAX_CORRECT_DEFAULT = 5
MIN_CORRECT_HARD = 3
MAX_CORRECT_HARD = 7

# Security Timeouts & Limits
CHALLENGE_TTL_SECONDS = 120  # 2 minutes TTL for challenge
TOKEN_TTL_SECONDS = 300       # 5 minutes TTL for verified token
MIN_TIME_TO_SUBMIT_SECONDS = 2.0  # Minimum 2 seconds to prevent automated instant clicks

# Rate Limiting
RATE_LIMIT_CHALLENGES_PER_MIN = 10   # 10 challenge requests per minute per IP
MAX_FAILED_VERIFICATIONS = 5         # 5 failed attempts in 10 minutes
FAILED_VERIFICATION_WINDOW = 600     # 10 minutes window
COOLDOWN_SECONDS = 300               # 5 minutes lockout after exceeding failed verifications
FAILED_LOGINS_FOR_HARD_GRID = 3      # 3 failed logins triggers 4x4 grid

# 8 Core Categories + 1 Distractor/Negative Category
CATEGORIES = {
    "traffic_lights": {
        "label": "traffic lights",
        "instruction": "Select all images with traffic lights",
    },
    "bicycles": {
        "label": "bicycles",
        "instruction": "Select all images with bicycles",
    },
    "cars": {
        "label": "cars",
        "instruction": "Select all images with cars",
    },
    "fire_hydrants": {
        "label": "fire hydrants",
        "instruction": "Select all images with fire hydrants",
    },
    "bridges": {
        "label": "bridges",
        "instruction": "Select all images with bridges",
    },
    "buses": {
        "label": "buses",
        "instruction": "Select all images with buses",
    },
    "motorcycles": {
        "label": "motorcycles",
        "instruction": "Select all images with motorcycles",
    },
    "crosswalks": {
        "label": "crosswalks",
        "instruction": "Select all images with crosswalks",
    },
}

NEGATIVES_CATEGORY = "negatives"
IMAGE_SIZE = (300, 300)
