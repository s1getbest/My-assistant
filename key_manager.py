import threading
import time
from google import genai
import config


class FallbackResponse:
    def __init__(self, text):
        self.text = text


class APIKeyManager:
    def __init__(self):
        self.lock = threading.Lock()
        self._keys = config.GEMINI_KEYS
        self._current_index = 0
        print(f"[KeyManager] Initialized with {len(self._keys)} API keys from dynamic pool")

    def get_client(self):
        """
        Retrieves a configured genai.Client instance with the current active API key.
        """
        with self.lock:
            if not self._keys:
                raise RuntimeError("No Gemini API keys configured. Set GEMINI_KEY_1 through GEMINI_KEY_N or GEMINI_API_KEY.")
            active_key = self._keys[self._current_index]
            return genai.Client(api_key=active_key)

    def rotate_key(self):
        with self.lock:
            if self._keys:
                self._current_index = (self._current_index + 1) % len(self._keys)
                print(f"[KeyManager] Rotated to key index: {self._current_index}")

    def _is_rate_limit_error(self, err_msg, error):
        return (
            "429" in err_msg
            or "RESOURCE_EXHAUSTED" in err_msg
            or "quota" in err_msg.lower()
            or str(getattr(error, "status_code", "")) == "429"
            or str(getattr(error, "code", "")) == "429"
        )

    def _is_high_demand_error(self, err_msg, error):
        return (
            "503" in err_msg
            or "UNAVAILABLE" in err_msg
            or "service unavailable" in err_msg.lower()
            or "high demand" in err_msg.lower()
            or str(getattr(error, "status_code", "")) == "503"
            or str(getattr(error, "code", "")) == "503"
        )

    def _safe_fallback_response(self, model):
        if model == config.MODEL_LITE:
            return FallbackResponse("Сервис ИИ временно перегружен. Попробуй еще раз через минуту.")
        return FallbackResponse("")

    def generate_content(self, model, contents, **kwargs):
        current_model = model
        last_error = None

        total_attempts = max(3, len(self._keys) + 1)
        for attempt in range(total_attempts):
            try:
                client = self.get_client()
                response = client.models.generate_content(
                    model=current_model,
                    contents=contents,
                    **kwargs
                )
                return response
            except Exception as e:
                last_error = e
                err_msg = str(e)

                if self._is_rate_limit_error(err_msg, e):
                    print(f"[KeyManager] 429 detected on attempt {attempt + 1}/{total_attempts}. Rotating key.")
                    self.rotate_key()
                    continue

                if self._is_high_demand_error(err_msg, e):
                    print(f"[KeyManager] 503/high demand detected. Falling back to {config.MODEL_LITE}.")
                    current_model = config.MODEL_LITE
                    time.sleep(1)
                    continue

                if str(getattr(e, "status_code", "")).startswith("5") or str(getattr(e, "code", "")).startswith("5"):
                    print(f"[KeyManager] Server-side Gemini error. Retrying with {current_model}.")
                    time.sleep(1)
                    continue

                print(f"[KeyManager] Direct API error (no rotation/no fallback): {err_msg}")
                return self._safe_fallback_response(current_model)

        print(f"[KeyManager] Exhausted retries. Last error: {last_error}")
        return self._safe_fallback_response(current_model)

# Singleton key manager instance
key_manager = APIKeyManager()
