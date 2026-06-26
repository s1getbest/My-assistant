import os
import pytz

# === ENVIRONMENT VARIABLES ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

# === GEMINI API KEYS ===
# Dynamic key pool: scan environment for all GEMINI_KEY_* variables
GEMINI_KEYS = []
for key, value in os.environ.items():
    if key.startswith("GEMINI_KEY_") and value:
        GEMINI_KEYS.append((key, value))
# Sort keys by suffix number to maintain consistent order
GEMINI_KEYS.sort(key=lambda kv: int(kv[0].split('_')[-1]) if kv[0].split('_')[-1].isdigit() else 999)
GEMINI_KEYS = [value for key, value in GEMINI_KEYS]

# Fallback to general GEMINI_API_KEY if no numbered keys found
if not GEMINI_KEYS:
    fallback_key = os.getenv("GEMINI_API_KEY")
    if fallback_key:
        GEMINI_KEYS.append(fallback_key)

# === MODEL ROUTING CONSTANTS ===
MODEL_COMPLEX = "gemini-3.5-flash"
MODEL_LITE = "gemini-3.1-flash-lite"

# === TIMEZONE ===
msk_tz = pytz.timezone("Europe/Moscow")
