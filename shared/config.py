import os
from dotenv import load_dotenv

load_dotenv()

# Admin console (/admin.html): session cookie signing
SECRET_KEY = os.getenv("SECRET_KEY", "")
# Login: set ADMIN_PASSWORD (plain, simplest for self-hosted) or ADMIN_PASSWORD_HASH (bcrypt, preferred for production)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
# Set true behind HTTPS in production so session cookie is Secure
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_ID = os.getenv("ADMIN_ID")
WEB_APP_URL = os.getenv("WEB_APP_URL")

# Stripe (optional; billing endpoints return 503 if secret key missing)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
# Fixed price IDs from Stripe Dashboard (one-time payment packs)
STRIPE_PRICE_ID_PACK_5 = os.getenv("STRIPE_PRICE_ID_PACK_5")
STRIPE_PRICE_ID_PACK_10 = os.getenv("STRIPE_PRICE_ID_PACK_10")
STRIPE_PRICE_ID_PACK_20 = os.getenv("STRIPE_PRICE_ID_PACK_20")


def stripe_checkout_price_map() -> dict[str, str]:
    """Maps price_key -> Stripe Price ID. Only keys with a configured ID are included."""
    m: dict[str, str] = {}
    if STRIPE_PRICE_ID_PACK_5:
        m["pack_5"] = STRIPE_PRICE_ID_PACK_5
    if STRIPE_PRICE_ID_PACK_10:
        m["pack_10"] = STRIPE_PRICE_ID_PACK_10
    if STRIPE_PRICE_ID_PACK_20:
        m["pack_20"] = STRIPE_PRICE_ID_PACK_20
    return m
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.1-flash-lite-preview")
# Optional: override paths to prompt files (see shared/prompts/*.txt defaults).
# PROMPT_IMAGE_OCR_EXTRACT_FILE, PROMPT_IMAGE_HAS_TEXT_FILE, PROMPT_IMAGE_NATIVE_TRANSLATE_FILE
# Admin-configurable limits (defaults for SystemSettings)
MAX_PAIRS_DEFAULT = 10
MAX_DESTINATIONS_PER_SOURCE_DEFAULT = 10
MAX_MESSAGE_LENGTH_DEFAULT = 4096
# Default per-translation prices (USD cents) for SystemSettings
CENTS_PER_TEXT_DEFAULT = 1
CENTS_PER_IMAGE_DEFAULT = 10

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is not set in .env")
