import os
import pytz

# === ENVIRONMENT VARIABLES ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

# === GEMINI API KEYS ===
GEMINI_KEY_1 = os.getenv("GEMINI_KEY_1")
GEMINI_KEY_2 = os.getenv("GEMINI_KEY_2")
GEMINI_KEY_3 = os.getenv("GEMINI_KEY_3")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # General fallback

# === MODEL ROUTING CONSTANTS ===
MODEL_COMPLEX = "gemini-3.5-flash"
MODEL_LITE = "gemini-3.1-flash-lite"

# === TIMEZONE ===
msk_tz = pytz.timezone("Europe/Moscow")
