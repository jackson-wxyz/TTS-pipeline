"""
Audio Lookup module: checks whether audio already exists for a URL
before generating TTS.

The EA/rationalist ecosystem has extensive audio coverage:
  - EA Forum: TYPE III AUDIO narrations (30+ karma → "All audio" feed,
    125+ karma → "Curated and Popular" feed) — we check both
  - LessWrong: TYPE III AUDIO narrations (30+ karma feed + curated via Buzzsprout)
  - The Nonlinear Library: AI narrations on Spotify, Apple Podcasts, etc.
  - Solenoid Entity: human narrations of ACX/SSC and LW Curated

Strategy: Instead of scraping individual post pages (which are React SPAs
and don't reliably expose audio links in raw HTML), we fetch the actual
TYPE III AUDIO RSS podcast feeds and match posts by URL slug.

The RSS feeds are cached locally so we don't re-download them for every
URL in a batch. Cache lifetime is configurable (default 24h).
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

import config

logger = logging.getLogger(__name__)

# ── Feed configuration ────────────────────────────────────────────────

# TYPE III AUDIO podcast feeds.
# Each entry: (feed_url, source_name, url_domain_match)
RSS_FEEDS = [
    (
        "https://forum-podcasts.effectivealtruism.org/ea-forum--all.rss",
        "eaforum-type3audio-all",
        "forum.effectivealtruism.org",
    ),
    (
        "https://forum-podcasts.effectivealtruism.org/ea-forum--curated-popular.rss",
        "eaforum-type3audio-curated",
        "forum.effectivealtruism.org",
    ),
    (
        "https://feeds.type3.audio/lesswrong--30-karma.rss",
        "lesswrong-type3audio-30karma",
        "lesswrong.com",
    ),
    (
        "https://rss.buzzsprout.com/2037297.rss",
        "lesswrong-type3audio-curated",
        "lesswrong.com",
    ),
]

# Cache settings
_CACHE_DIR = os.path.join(config.OUTPUT_DIR, ".audio_lookup_cache")
_CACHE_LIFETIME_SECONDS = 24 * 60 * 60  # 24 hours


@dataclass
class AudioLookupResult:
    """Result of checking for existing audio."""
    url: str
    audio_found: bool = False
    audio_url: str = ""
    audio_source: str = ""   # e.g. "lesswrong-type3audio", "eaforum-type3audio"
    audio_title: str = ""    # title from the podcast feed (for verification)
    error: str = ""


def check_existing_audio(url: str) -> AudioLookupResult:
    """
    Check if audio already exists for the given URL by looking it up
    in the TYPE III AUDIO podcast RSS feeds.

    The approach:
      1. Determine which feed(s) to check based on the URL's domain.
      2. Fetch (or use cached) RSS feed XML.
      3. Parse <item> entries, matching by <link> or <guid> against
         the post URL or its slug.
      4. If matched, return the <enclosure> audio URL.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")
    slug = _extract_slug(url)

    for feed_url, source_name, domain_match in RSS_FEEDS:
        if domain_match not in domain:
            continue

        logger.info(f"  Checking {source_name} RSS feed for existing audio...")

        try:
            entries = _get_feed_entries(feed_url, source_name)

            if not entries:
                logger.info(f"  No entries loaded from {source_name} feed")
                continue

            # Try to match the URL against feed entries
            match = _match_url_in_feed(url, slug, entries)

            if match:
                logger.info(f"  Found existing audio from {source_name}: "
                            f"{match['title']!r}")
                return AudioLookupResult(
                    url=url,
                    audio_found=True,
                    audio_url=match["audio_url"],
                    audio_source=source_name,
                    audio_title=match["title"],
                )
            else:
                logger.info(f"  No match in {source_name} feed "
                            f"({len(entries)} entries checked)")

        except Exception as e:
            logger.warning(f"  Audio lookup failed ({source_name}): {e}")

    return AudioLookupResult(url=url, audio_found=False)


# ── Feed fetching & caching ──────────────────────────────────────────

def _get_feed_entries(feed_url: str, source_name: str) -> list[dict]:
    """
    Get parsed entries from an RSS feed, using a local cache.

    Returns a list of dicts, each with keys:
      - title: episode title
      - link: the original post URL
      - guid: the entry's guid
      - audio_url: the enclosure URL (MP3)
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)

    # Cache filename based on source name
    cache_file = os.path.join(_CACHE_DIR, f"{source_name}.json")

    # Check if cache is fresh enough
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < _CACHE_LIFETIME_SECONDS:
            try:
                with open(cache_file, "r") as f:
                    entries = json.load(f)
                logger.debug(f"  Using cached feed: {len(entries)} entries "
                             f"({age/3600:.1f}h old)")
                return entries
            except (json.JSONDecodeError, IOError):
                pass  # cache corrupt, re-fetch

    # Fetch the feed
    logger.info(f"  Downloading RSS feed: {feed_url}")
    try:
        resp = requests.get(feed_url, timeout=30, headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml",
        })
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"  Failed to download feed: {e}")
        # Return stale cache if available
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    # Parse the RSS XML
    entries = _parse_rss_feed(resp.text)

    # Save to cache
    try:
        with open(cache_file, "w") as f:
            json.dump(entries, f)
        logger.info(f"  Cached {len(entries)} entries from {source_name}")
    except IOError as e:
        logger.debug(f"  Could not write cache: {e}")

    return entries


def _parse_rss_feed(xml_text: str) -> list[dict]:
    """Parse an RSS 2.0 feed and extract entries."""
    entries = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"  RSS parse error: {e}")
        return []

    # Handle potential namespace prefixes
    # Standard RSS 2.0: rss > channel > item
    channel = root.find("channel")
    if channel is None:
        # Try with Atom namespace or just look for items anywhere
        items = root.findall(".//item")
    else:
        items = channel.findall("item")

    for item in items:
        entry = {}

        # Title
        title_el = item.find("title")
        entry["title"] = title_el.text.strip() if title_el is not None and title_el.text else ""

        # Link (the original post URL)
        link_el = item.find("link")
        entry["link"] = link_el.text.strip() if link_el is not None and link_el.text else ""

        # GUID (often the post URL, sometimes a unique ID)
        guid_el = item.find("guid")
        entry["guid"] = guid_el.text.strip() if guid_el is not None and guid_el.text else ""

        # Enclosure (the audio file URL)
        enclosure_el = item.find("enclosure")
        if enclosure_el is not None:
            entry["audio_url"] = enclosure_el.get("url", "")
        else:
            entry["audio_url"] = ""

        # Only keep entries that have audio
        if entry["audio_url"]:
            entries.append(entry)

    return entries


# ── URL matching ──────────────────────────────────────────────────────

def _extract_slug(url: str) -> str:
    """
    Extract the post slug from a URL.

    Examples:
      https://forum.effectivealtruism.org/posts/ABC123/my-great-post
        → "my-great-post"
      https://www.lesswrong.com/posts/XYZ789/against-something
        → "against-something"
      https://www.lesswrong.com/posts/XYZ789
        → "XYZ789"
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    # LW and EAF use /posts/<id>/<slug> format
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "posts":
        return parts[-1]  # the slug
    elif len(parts) >= 2 and parts[0] == "posts":
        return parts[1]   # the post ID (no slug in URL)

    # Fallback: last path segment
    return parts[-1] if parts else ""


def _match_url_in_feed(url: str, slug: str,
                       entries: list[dict]) -> dict | None:
    """
    Try to match a post URL against RSS feed entries.

    Matching strategy (in order of specificity):
      1. Exact URL match in link or guid
      2. URL contains the slug and feed link/guid also contains it
      3. Slug match in feed link (fuzzy — for URL variations)
    """
    url_lower = url.lower().rstrip("/")

    for entry in entries:
        link_lower = entry.get("link", "").lower().rstrip("/")
        guid_lower = entry.get("guid", "").lower().rstrip("/")

        # Strategy 1: Exact URL match
        if url_lower == link_lower or url_lower == guid_lower:
            return entry

        # Strategy 2: URL path match (ignoring domain differences like
        # www. vs non-www, or greaterwrong.com vs lesswrong.com)
        url_path = urlparse(url_lower).path.rstrip("/")
        link_path = urlparse(link_lower).path.rstrip("/")
        if url_path and link_path and url_path == link_path:
            return entry

    # Strategy 3: Slug-based fuzzy match
    if slug and len(slug) > 5:  # avoid matching very short slugs
        slug_lower = slug.lower()
        for entry in entries:
            link_lower = entry.get("link", "").lower()
            guid_lower = entry.get("guid", "").lower()
            # Check if the slug appears in the feed entry's link/guid
            if slug_lower in link_lower or slug_lower in guid_lower:
                return entry

    return None


# ── Manual cache refresh ──────────────────────────────────────────────

def refresh_feed_cache():
    """Force-refresh all RSS feed caches. Useful before a big batch."""
    for feed_url, source_name, _ in RSS_FEEDS:
        logger.info(f"Refreshing {source_name} feed cache...")
        # Delete existing cache to force re-download
        cache_file = os.path.join(_CACHE_DIR, f"{source_name}.json")
        if os.path.exists(cache_file):
            os.remove(cache_file)
        entries = _get_feed_entries(feed_url, source_name)
        logger.info(f"  {source_name}: {len(entries)} entries cached")
