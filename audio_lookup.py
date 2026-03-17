"""
Audio Lookup module: checks whether audio already exists for a URL
before generating TTS.

The EA/rationalist ecosystem has extensive audio coverage:
  - LessWrong: curated posts and 125+ karma posts have official audio
  - EA Forum: TYPE III AUDIO narrations (30+ karma → "All audio" feed,
    125+ karma → "Curated and Popular" feed)
  - The Nonlinear Library: AI narrations on Spotify, Apple Podcasts, etc.
  - Solenoid Entity: human narrations of ACX/SSC and LW Curated

This module checks for embedded audio on the source page itself.
For everything else, we skip straight to TTS generation.

The design is a registry of URL-pattern → lookup-function mappings,
so new sources can be added easily.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class AudioLookupResult:
    """Result of checking for existing audio."""
    url: str
    audio_found: bool = False
    audio_url: str = ""
    audio_source: str = ""   # e.g. "lesswrong-official", "eaforum-type3audio"
    error: str = ""


def check_existing_audio(url: str) -> AudioLookupResult:
    """
    Check if audio already exists for the given URL.

    Returns an AudioLookupResult. If audio_found is True, audio_url
    contains a direct link to the MP3/audio file.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    # Check each registered lookup function
    for pattern_fn, lookup_fn, source_name in _LOOKUP_REGISTRY:
        if pattern_fn(domain, url):
            logger.info(f"  Checking {source_name} for existing audio...")
            try:
                result = lookup_fn(url, domain)
                if result.audio_found:
                    logger.info(f"  Found existing audio from {source_name}: "
                                f"{result.audio_url[:80]}...")
                    return result
                else:
                    logger.info(f"  No audio found via {source_name}")
            except Exception as e:
                logger.warning(f"  Audio lookup failed ({source_name}): {e}")

    return AudioLookupResult(url=url, audio_found=False)


# ── LessWrong / EA Forum audio lookup ─────────────────────────────────

def _is_lesswrong(domain: str, url: str) -> bool:
    return "lesswrong.com" in domain


def _is_eaforum(domain: str, url: str) -> bool:
    return "forum.effectivealtruism.org" in domain or "ea.greaterwrong.com" in domain


def _check_forum_audio(url: str, domain: str) -> AudioLookupResult:
    """
    Check LessWrong or EA Forum for embedded audio.

    Both forums embed audio players for posts that have been narrated.
    The audio URL is typically in a <source> tag within an <audio> element,
    or referenced in the page's JSON data.

    Strategy:
      1. Fetch the page HTML
      2. Look for <audio> tags or known audio player patterns
      3. Extract the audio source URL
    """
    try:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = requests.get(url, headers=headers, timeout=config.FETCH_TIMEOUT,
                            allow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        # Look for audio source URLs in the HTML
        # LW/EAF use various audio hosting services
        audio_patterns = [
            # Direct <audio> or <source> tags
            r'<source\s+src="([^"]+\.(?:mp3|m4a|ogg|wav))"',
            r'<audio[^>]+src="([^"]+\.(?:mp3|m4a|ogg|wav))"',
            # Type III Audio / podcast-style embeds
            r'"audioUrl"\s*:\s*"([^"]+)"',
            r'"audio_url"\s*:\s*"([^"]+)"',
            # Buzzsprout (commonly used by EA Forum)
            r'(https://www\.buzzsprout\.com/[^"\'<\s]+\.mp3)',
            # Generic audio file URLs in the page
            r'(https://[^"\'<\s]+/audio/[^"\'<\s]+\.mp3)',
        ]

        for pattern in audio_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                audio_url = match.group(1)
                source = "lesswrong" if "lesswrong" in domain else "eaforum"
                return AudioLookupResult(
                    url=url, audio_found=True,
                    audio_url=audio_url,
                    audio_source=f"{source}-embedded"
                )

        return AudioLookupResult(url=url, audio_found=False)

    except Exception as e:
        return AudioLookupResult(url=url, audio_found=False,
                                 error=str(e))


# ── Lookup registry ───────────────────────────────────────────────────
# Each entry is (pattern_function, lookup_function, source_name).
# Add new sources here as needed.

_LOOKUP_REGISTRY = [
    (_is_lesswrong, _check_forum_audio, "LessWrong"),
    (_is_eaforum, _check_forum_audio, "EA Forum"),
]
