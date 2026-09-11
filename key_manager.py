import threading
import time
from google import genai
import config
from logging_config import get_logger

logger = get_logger(__name__)


class FallbackResponse:
    def __init__(self, text):
        self.text = text


class GenerationResult:
    """
    Thin wrapper around whatever generate_content() got back (a real
    genai response or a FallbackResponse), so every existing call site's
    `response.text` keeps working unchanged, while new callers that care
    (the Telegram progress-status UX) can also check `used_fallback` /
    `attempts` to tell the user a key/model swap happened along the way.
    """
    def __init__(self, text, requested_model, model_used, attempts, used_fallback):
        self.text = text
        self.requested_model = requested_model
        self.model_used = model_used
        self.attempts = attempts
        self.used_fallback = used_fallback


class APIKeyManager:
    # How long a key that looks permanently invalid/unauthorized (not just
    # rate-limited) is excluded from rotation before we try it again. Long
    # enough to stop hammering a genuinely dead/banned key on every message,
    # short enough to self-heal if it turns out to have been a transient
    # billing/auth hiccup rather than a truly dead key.
    DEAD_KEY_COOLDOWN_SECONDS = 3600

    def __init__(self):
        self.lock = threading.Lock()
        self._keys = config.GEMINI_KEYS
        self._current_index = 0
        self._dead_until = {}  # key index -> unix timestamp it becomes eligible again
        logger.info(f"[KeyManager] Initialized with {len(self._keys)} API keys from dynamic pool")

    def _is_key_available(self, index):
        deadline = self._dead_until.get(index)
        return deadline is None or time.time() >= deadline

    def get_client(self):
        """
        Retrieves a configured genai.Client instance, preferring the
        current key but skipping any key currently in its dead-key
        cooldown (see _mark_current_key_dead). Falls back to using the
        current key anyway if every key happens to be in cooldown at once,
        since attempting and failing is more useful than refusing outright.
        """
        with self.lock:
            if not self._keys:
                raise RuntimeError("No Gemini API keys configured. Set GEMINI_KEY_1 through GEMINI_KEY_N or GEMINI_API_KEY.")
            n = len(self._keys)
            for offset in range(n):
                idx = (self._current_index + offset) % n
                if self._is_key_available(idx):
                    self._current_index = idx
                    return genai.Client(api_key=self._keys[idx])
            return genai.Client(api_key=self._keys[self._current_index])

    def rotate_key(self):
        with self.lock:
            if self._keys:
                self._current_index = (self._current_index + 1) % len(self._keys)
                logger.info(f"[KeyManager] Rotated to key index: {self._current_index}")

    def _mark_current_key_dead(self):
        with self.lock:
            if self._keys:
                self._dead_until[self._current_index] = time.time() + self.DEAD_KEY_COOLDOWN_SECONDS
                logger.error(
                    f"[KeyManager] Key index {self._current_index} looks invalid/unauthorized - "
                    f"excluding it from rotation for {self.DEAD_KEY_COOLDOWN_SECONDS // 60} minutes."
                )

    def _is_fatal_key_error(self, err_msg, error):
        """
        Distinguishes "this specific key is bad" (invalid/revoked/no
        permission) from transient errors like rate limits or server
        overload, which just need a retry/rotation, not exclusion.
        """
        lowered = err_msg.lower()
        return (
            "api_key_invalid" in lowered
            or "api key not valid" in lowered
            or "permission_denied" in lowered
            or "unauthenticated" in lowered
            or str(getattr(error, "status_code", "")) in ("401", "403")
            or str(getattr(error, "code", "")) in ("401", "403")
        )

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

    def _safe_fallback_response(self, requested_model, current_model, attempts):
        text = "The AI service is temporarily overloaded. Please try again in a minute." if current_model == config.MODEL_LITE else ""
        return GenerationResult(
            text=text,
            requested_model=requested_model,
            model_used=current_model,
            attempts=attempts,
            used_fallback=True,
        )

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
                return GenerationResult(
                    text=getattr(response, "text", None),
                    requested_model=model,
                    model_used=current_model,
                    attempts=attempt + 1,
                    used_fallback=(attempt > 0 or current_model != model),
                )
            except Exception as e:
                last_error = e
                err_msg = str(e)

                if self._is_fatal_key_error(err_msg, e):
                    logger.warning(f"[KeyManager] Fatal key error on attempt {attempt + 1}/{total_attempts}: {err_msg}")
                    self._mark_current_key_dead()
                    self.rotate_key()
                    continue

                if self._is_rate_limit_error(err_msg, e):
                    logger.warning(f"[KeyManager] 429 detected on attempt {attempt + 1}/{total_attempts}. Rotating key.")
                    self.rotate_key()
                    continue

                if self._is_high_demand_error(err_msg, e):
                    logger.warning(f"[KeyManager] 503/high demand detected. Falling back to {config.MODEL_LITE}.")
                    current_model = config.MODEL_LITE
                    time.sleep(1)
                    continue

                if str(getattr(e, "status_code", "")).startswith("5") or str(getattr(e, "code", "")).startswith("5"):
                    logger.warning(f"[KeyManager] Server-side Gemini error. Retrying with {current_model}.")
                    time.sleep(1)
                    continue

                logger.error(f"[KeyManager] Direct API error (no rotation/no fallback): {err_msg}")
                return self._safe_fallback_response(model, current_model, attempt + 1)

        logger.error(f"[KeyManager] Exhausted retries. Last error: {last_error}")
        return self._safe_fallback_response(model, current_model, total_attempts)

# Singleton key manager instance
key_manager = APIKeyManager()
