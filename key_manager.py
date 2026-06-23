import threading
from google import genai
import config

class APIKeyManager:
    def __init__(self):
        self.lock = threading.Lock()
        self._keys = []
        # Load the 3 distinct keys
        for key_val in [config.GEMINI_KEY_1, config.GEMINI_KEY_2, config.GEMINI_KEY_3]:
            if key_val:
                self._keys.append(key_val)
        
        # Fallback to general/fallback key if specific keys aren't configured
        if not self._keys and config.GEMINI_API_KEY:
            self._keys.append(config.GEMINI_API_KEY)
            
        self._current_index = 0

    def get_client(self):
        """
        Retrieves a configured genai.Client instance with the current active API key.
        """
        with self.lock:
            if not self._keys:
                raise RuntimeError("No Gemini API keys configured. Set GEMINI_KEY_1, GEMINI_KEY_2, GEMINI_KEY_3 or GEMINI_API_KEY.")
            active_key = self._keys[self._current_index]
            return genai.Client(api_key=active_key)

    def rotate_key(self):
        """
        Rotates active API key index in a thread-safe manner.
        """
        with self.lock:
            if self._keys:
                self._current_index = (self._current_index + 1) % len(self._keys)
                print(f"[KeyManager] Rotated to key index: {self._current_index}")

    def generate_content(self, model, contents, **kwargs):
        """
        Wrapper around client.models.generate_content that catches 429 / resource exhausted
        errors, rotates keys thread-safely, and retries the request up to 3 times.
        """
        last_error = None
        for attempt in range(3):
            try:
                client = self.get_client()
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    **kwargs
                )
                return response
            except Exception as e:
                last_error = e
                err_msg = str(e)
                # Check for standard Google 429 or RESOURCE_EXHAUSTED / quota error
                is_quota_issue = "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower()
                if is_quota_issue and len(self._keys) > 1:
                    print(f"[KeyManager] Rate limit / Quota issue detected. Attempt {attempt + 1}/3. Rotating API key.")
                    self.rotate_key()
                else:
                    print(f"[KeyManager] Direct API error (no rotation): {err_msg}")
                    raise e
        raise last_error

# Singleton key manager instance
key_manager = APIKeyManager()
