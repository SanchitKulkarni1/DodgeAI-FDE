
import os
import logging
import google.genai as genai
from google.genai import types  

log = logging.getLogger(__name__)
MODEL= "google-flash-latest"

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise EnvironmentError(
        "GEMINI_API_KEY environment variable is not set. "
        "Get a free key at https://ai.google.dev and export it before running."
    )

gemini: genai.Client = genai.Client(api_key=api_key)

log.info("google-genai client initialised  model=%s", MODEL)

# Re-export types so callers don't need a second import
__all__ = ["gemini", "MODEL", "types"]