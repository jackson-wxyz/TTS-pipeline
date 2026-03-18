"""
Configuration for the TTS Audio Pipeline.

Adjust these settings to match your local setup:
  - Kokoro-FastAPI endpoint (default: localhost:8880)
  - LM Studio endpoint (for text preprocessing, optional)
  - Podcast feed settings (title, URL, etc.)
"""

import os as _os

# ── Kokoro-FastAPI TTS Server ────────────────────────────────────────
KOKORO_BASE_URL = "http://localhost:8880/v1" #or "http://192.168.10.75:1234/v1"??
KOKORO_VOICE = "af_heart"  # see Kokoro docs for voice options
KOKORO_SPEED = 1.0         # 1.0 = normal, 1.2 = slightly faster
KOKORO_RESPONSE_FORMAT = "mp3"

# ── LM Studio (optional, for AI text preprocessing) ─────────────────
LM_STUDIO_BASE_URL = "http://192.168.10.75:1234/v1"
LM_STUDIO_API_KEY = "sk-lm-FaiipYw6:FVNsw8bZvEgxp61tn10X"
CHAT_MODEL = "qwen/qwen3.5-35b-a3b"
USE_LLM_PREPROCESSING = True  # set True to use LLM for audio-optimized text cleanup

# ── Fetcher Settings ─────────────────────────────────────────────────
FETCH_TIMEOUT = 15  # seconds per URL
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Max article chars to send to TTS (Kokoro handles long text fine,
# but very long articles can produce huge MP3s)
MAX_ARTICLE_CHARS = 100000  # ~25k words, ~2+ hours of audio

# ── Podcast Feed Settings ────────────────────────────────────────────
PODCAST_TITLE = "Eudæmonia Radio"
PODCAST_DESCRIPTION = "Auto-generated audio from articles, blog posts, and essays."
PODCAST_AUTHOR = "TTS Pipeline"
PODCAST_LANGUAGE = "en"
PODCAST_IMAGE_URL = "https://jacksonw.xyz/games/jadwiga_radio.png"  # optional: URL to a podcast cover image

# Base URL where the feed and MP3s will be served from.
# For local-only use, this is your desktop's LAN IP + port.
# For remote access, point this at wherever you host the files.
FEED_BASE_URL = "http://192.168.10.75:8888" #or "http://localhost:8888"?

# Port for the built-in HTTP server that serves the feed
FEED_SERVER_PORT = 8888

# ── Output Directories ───────────────────────────────────────────────
_BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))
OUTPUT_DIR = _os.path.join(_BASE_DIR, "output")
AUDIO_DIR = _os.path.join(OUTPUT_DIR, "audio")
FEED_DIR = _os.path.join(OUTPUT_DIR, "feed")
DB_PATH = _os.path.join(OUTPUT_DIR, "pipeline.db")

# ── Pipeline Settings ────────────────────────────────────────────────
# Maximum number of concurrent TTS requests (Kokoro is fast, but
# you may want to limit this if running other GPU tasks)
MAX_CONCURRENT_TTS = 1

# Retry settings
MAX_RETRIES = 3
RETRY_DELAY = 5.0
