import hashlib
import os
import pytz
from logging_config import get_logger

logger = get_logger(__name__)

# === ENVIRONMENT VARIABLES ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

# === WEBHOOK SECRETS ===
# The bot token must never appear in the webhook URL: Render/Flask access logs,
# proxies and browser history would all end up holding a value that grants
# full control over the bot. Instead, derive a URL path segment and a
# Telegram "secret_token" (sent back on every update as the
# X-Telegram-Bot-Api-Secret-Token header, see Telegram Bot API docs)
# deterministically from the real token via SHA-256. Both are stable across
# restarts/deploys without needing a new environment variable, and neither
# can be used to reconstruct the real token or call the Telegram API.
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "https://my-assistant-k7rq.onrender.com")
if TELEGRAM_TOKEN:
    WEBHOOK_PATH_SECRET = hashlib.sha256(f"webhook-path:{TELEGRAM_TOKEN}".encode()).hexdigest()
    WEBHOOK_SECRET_TOKEN = hashlib.sha256(f"webhook-secret-token:{TELEGRAM_TOKEN}".encode()).hexdigest()
else:
    WEBHOOK_PATH_SECRET = None
    WEBHOOK_SECRET_TOKEN = None

# === EXTERNAL / DASHBOARD ACCESS SECRETS ===
# No insecure defaults: if these are not configured, the corresponding
# endpoints are disabled (fail closed) rather than silently using a
# well-known fallback value that anyone reading this open-source repo could
# use to authenticate.
EXTERNAL_API_KEY = os.getenv("EXTERNAL_API_KEY")
if not EXTERNAL_API_KEY:
    logger.warning(
        "[Config] EXTERNAL_API_KEY is not set - /api/webhook/external will reject all requests "
        "until it is configured."
    )

DASHBOARD_ACCESS_KEY = os.getenv("DASHBOARD_ACCESS_KEY")
if not DASHBOARD_ACCESS_KEY:
    logger.warning(
        "[Config] DASHBOARD_ACCESS_KEY is not set - the '/' dashboard will reject all requests "
        "until it is configured."
    )

# === GEMINI API KEYS ===
# Dynamic key pool: scan environment for all GEMINI_KEY_* variables
# Validate keys: must start with "AIzaSy" OR "AQ." (new Google AI Studio format) and not be empty/None
GEMINI_KEYS = []
for key, value in os.environ.items():
    if key.startswith("GEMINI_KEY_") and value and value.strip():
        # Clean key: strip quotes and whitespace
        cleaned_key = value.strip().strip("'\" ")
        # Validate key format (Gemini keys start with "AIzaSy" or "AQ.")
        if cleaned_key.startswith("AIzaSy") or cleaned_key.startswith("AQ."):
            GEMINI_KEYS.append((key, cleaned_key))
        else:
            logger.warning(f"[Config] Skipping invalid key format for {key}: does not start with AIzaSy or AQ.")
# Sort keys by suffix number to maintain consistent order
GEMINI_KEYS.sort(key=lambda kv: int(kv[0].split('_')[-1]) if kv[0].split('_')[-1].isdigit() else 999)
GEMINI_KEYS = [value for key, value in GEMINI_KEYS]

# Fallback to general GEMINI_API_KEY if no numbered keys found
if not GEMINI_KEYS:
    fallback_key = os.getenv("GEMINI_API_KEY")
    if fallback_key and fallback_key.strip():
        cleaned_key = fallback_key.strip().strip("'\" ")
        if cleaned_key.startswith("AIzaSy") or cleaned_key.startswith("AQ."):
            GEMINI_KEYS.append(cleaned_key)
            logger.info("[Config] Using fallback GEMINI_API_KEY")
        else:
            logger.warning("[Config] Warning: Fallback GEMINI_API_KEY has invalid format")
    else:
        logger.warning("[Config] Warning: No valid Gemini API keys found. Please set GEMINI_KEY_1 through GEMINI_KEY_N or GEMINI_API_KEY with valid keys starting with 'AIzaSy' or 'AQ.'")

# === MODEL ROUTING CONSTANTS ===
MODEL_COMPLEX = "gemini-3.5-flash"
MODEL_LITE = "gemini-3.1-flash-lite"

# === TIMEZONE ===
msk_tz = pytz.timezone("Europe/Moscow")
