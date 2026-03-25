import os
import logging
from pathlib import Path
from dotenv import load_dotenv
import google.genai as genai
from google.genai import types  
from google.genai.errors import APIError

# Load environment variables from .env file
load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)
MODEL = "gemini-flash-latest"

# Extract all unique GEMINI_API_KEY environment variables (e.g., GEMINI_API_KEY, GEMINI_API_KEY_1, GEMINI_API_KEY_2)
api_keys = []
for k, v in os.environ.items():
    if k.startswith("GEMINI_API_KEY") and v.strip():
        if v.strip() not in api_keys:
            api_keys.append(v.strip())

if not api_keys:
    raise EnvironmentError(
        "No GEMINI_API_KEY environment variables found. "
        "Get a free key at https://ai.google.dev and export it before running."
    )

class GeminiRoundRobinClient:
    """A proxy client that cycles through available API keys when hitting rate limits."""
    
    def __init__(self, keys):
        self._keys = keys
        self._current_index = 0
        self._init_client()
        self.models = self._ModelsProxy(self)
        
    def _init_client(self):
        self._client = genai.Client(api_key=self._keys[self._current_index])
        
    def _rotate_key(self):
        old_index = self._current_index
        self._current_index = (self._current_index + 1) % len(self._keys)
        log.warning(f"Gemini API rate limit hit! Switching from key {old_index + 1} to key {self._current_index + 1} (out of {len(self._keys)}).")
        self._init_client()

    class _ModelsProxy:
        def __init__(self, parent):
            self.parent = parent

        def generate_content(self, *args, **kwargs):
            attempts = 0
            max_attempts = len(self.parent._keys)
            
            while attempts < max_attempts:
                try:
                    return self.parent._client.models.generate_content(*args, **kwargs)
                except APIError as e:
                    # 429 Resource Exhausted (Rate limits)
                    if e.code == 429:
                        attempts += 1
                        if attempts >= max_attempts:
                            log.error("All Gemini API keys exhausted their rate limits.")
                            raise e
                        self.parent._rotate_key()
                    else:
                        raise e

gemini = GeminiRoundRobinClient(api_keys)

log.info("google-genai round-robin client initialised with %d keys, model=%s", len(api_keys), MODEL)

# Re-export types so callers don't need a second import
__all__ = ["gemini", "MODEL", "types"]