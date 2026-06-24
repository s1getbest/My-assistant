import threading
import time
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
        Wrapper around client.models.generate_content that manages:
        - Thread-safe key rotation on 429 Rate Limit/Quota errors.
        - Automatic fallback from MODEL_COMPLEX to MODEL_LITE on 503 / 5xx errors.
        - Retries up to 3 times total.
        """
        current_model = model
        last_error = None
        
        for attempt in range(3):
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
                
                # Check for standard Google 429 or RESOURCE_EXHAUSTED / quota error
                is_quota_issue = "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower()
                
                # Check for 503 / 5xx / Unavailable / Internal Server Error
                is_503_or_5xx = (
                    "503" in err_msg or 
                    "500" in err_msg or 
                    "502" in err_msg or 
                    "504" in err_msg or 
                    "UNAVAILABLE" in err_msg or 
                    "INTERNAL" in err_msg or 
                    "service unavailable" in err_msg.lower() or
                    "internal server error" in err_msg.lower() or
                    (hasattr(e, 'code') and str(getattr(e, 'code')).startswith('5')) or
                    (hasattr(e, 'status_code') and str(getattr(e, 'status_code')).startswith('5'))
                )
                
                if is_quota_issue:
                    if len(self._keys) > 1:
                        print(f"[KeyManager] Rate limit / Quota issue (429) detected. Attempt {attempt + 1}/3. Rotating API key.")
                        self.rotate_key()
                        continue
                    else:
                        print(f"[KeyManager] Rate limit (429) hit, but only 1 key available. Raising error.")
                        raise e
                        
                elif is_503_or_5xx:
                    if current_model == config.MODEL_COMPLEX:
                        print(f"[AI] 503/5xx error with {config.MODEL_COMPLEX}, falling back to MODEL_LITE")
                        # Try the request immediately using MODEL_LITE
                        try:
                            print(f"[KeyManager] Retrying immediately with fallback model: {config.MODEL_LITE}")
                            response = client.models.generate_content(
                                model=config.MODEL_LITE,
                                contents=contents,
                                **kwargs
                            )
                            # Fallback succeeded, change our tracking model and return the result
                            current_model = config.MODEL_LITE
                            return response
                        except Exception as fallback_e:
                            print(f"[KeyManager] Fallback to {config.MODEL_LITE} also failed: {fallback_e}")
                            last_error = fallback_e
                            # Let the outer loop retry with the next key/attempt
                    else:
                        print(f"[KeyManager] 503/5xx error on MODEL_LITE or non-complex model: {err_msg}. Retrying in next attempt...")
                        time.sleep(1)
                else:
                    print(f"[KeyManager] Direct API error (no rotation/no fallback): {err_msg}")
                    raise e
                    
        raise last_error

# Singleton key manager instance
key_manager = APIKeyManager()
