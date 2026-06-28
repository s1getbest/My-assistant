import os
import pytz

# === ENVIRONMENT VARIABLES ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

# === GEMINI API KEYS ===
# Dynamic key pool: scan environment for all GEMINI_KEY_* variables
# Validate keys: must start with "AIzaSy" and not be empty/None
GEMINI_KEYS = []
for key, value in os.environ.items():
    if key.startswith("GEMINI_KEY_") and value and value.strip():
        # Validate key format (Gemini keys start with "AIzaSy")
        if value.strip().startswith("AIzaSy"):
            GEMINI_KEYS.append((key, value.strip()))
        else:
            print(f"[Config] Skipping invalid key format for {key}: does not start with AIzaSy")
# Sort keys by suffix number to maintain consistent order
GEMINI_KEYS.sort(key=lambda kv: int(kv[0].split('_')[-1]) if kv[0].split('_')[-1].isdigit() else 999)
GEMINI_KEYS = [value for key, value in GEMINI_KEYS]

# Fallback to general GEMINI_API_KEY if no numbered keys found
if not GEMINI_KEYS:
    fallback_key = os.getenv("GEMINI_API_KEY")
    if fallback_key and fallback_key.strip() and fallback_key.strip().startswith("AIzaSy"):
        GEMINI_KEYS.append(fallback_key.strip())
        print("[Config] Using fallback GEMINI_API_KEY")
    else:
        print("[Config] Warning: No valid Gemini API keys found. Please set GEMINI_KEY_1 through GEMINI_KEY_N or GEMINI_API_KEY with valid keys starting with 'AIzaSy'")

# === MODEL ROUTING CONSTANTS ===
MODEL_COMPLEX = "gemini-3.5-flash"
MODEL_LITE = "gemini-3.1-flash-lite"

# === TIMEZONE ===
msk_tz = pytz.timezone("Europe/Moscow")
