"""
RSS Podcast Feed Generator.

Generates a valid podcast RSS feed from the audio files in the output
directory. Pocket Casts (or any podcast app) subscribes to the feed URL
and picks up new episodes on refresh.

The feed is a standard RSS 2.0 document with iTunes podcast extensions,
which means it works with essentially every podcast app.

Usage:
    The pipeline calls regenerate_feed() after generating new audio.
    A simple HTTP server (see serve.py or `python -m http.server`) serves
    the feed.xml and audio files.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, tostring, indent, register_namespace

import config

logger = logging.getLogger(__name__)

# iTunes namespace for podcast-specific tags
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"

# Register namespaces so ElementTree uses clean prefixes
register_namespace("itunes", ITUNES_NS)
register_namespace("atom", ATOM_NS)


def regenerate_feed() -> str:
    """
    Regenerate the podcast RSS feed from the pipeline database.

    Reads all completed episodes from the SQLite database and writes
    a feed.xml file. Returns the path to the generated feed.
    """
    os.makedirs(config.FEED_DIR, exist_ok=True)
    feed_path = os.path.join(config.FEED_DIR, "feed.xml")

    episodes = _load_episodes()
    xml_bytes = _build_feed_xml(episodes)

    with open(feed_path, "wb") as f:
        f.write(xml_bytes)

    logger.info(f"Feed regenerated: {len(episodes)} episodes → {feed_path}")
    return feed_path


def _load_episodes() -> list[dict]:
    """Load completed episodes from the pipeline database."""
    if not os.path.exists(config.DB_PATH):
        return []

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT url, title, author, audio_path, audio_source,
                   word_count, estimated_minutes, created_at, file_size_bytes
            FROM episodes
            WHERE audio_path IS NOT NULL
            ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return []
    finally:
        conn.close()


def _build_feed_xml(episodes: list[dict]) -> bytes:
    """Build the RSS XML document."""
    # Root element with namespaces
    rss = Element("rss", {
        "version": "2.0",
    })

    channel = SubElement(rss, "channel")

    # Channel metadata
    SubElement(channel, "title").text = config.PODCAST_TITLE
    SubElement(channel, "description").text = config.PODCAST_DESCRIPTION
    SubElement(channel, "language").text = config.PODCAST_LANGUAGE
    SubElement(channel, "link").text = config.FEED_BASE_URL

    # Atom self-link (required by some podcast apps)
    feed_url = f"{config.FEED_BASE_URL}/feed/feed.xml"
    SubElement(channel, f"{{{ATOM_NS}}}link", {
        "href": feed_url,
        "rel": "self",
        "type": "application/rss+xml",
    })

    # iTunes-specific channel tags
    SubElement(channel, f"{{{ITUNES_NS}}}author").text = config.PODCAST_AUTHOR
    SubElement(channel, f"{{{ITUNES_NS}}}summary").text = config.PODCAST_DESCRIPTION
    SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "no"

    owner = SubElement(channel, f"{{{ITUNES_NS}}}owner")
    SubElement(owner, f"{{{ITUNES_NS}}}name").text = config.PODCAST_AUTHOR

    if config.PODCAST_IMAGE_URL:
        SubElement(channel, f"{{{ITUNES_NS}}}image", {
            "href": config.PODCAST_IMAGE_URL,
        })

    # Last build date
    SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    # Episodes (items)
    for ep in episodes:
        item = SubElement(channel, "item")

        title = ep.get("title", "Untitled")
        SubElement(item, "title").text = title

        # Description with source info
        desc_parts = []
        if ep.get("author"):
            desc_parts.append(f"By {ep['author']}")
        if ep.get("url"):
            desc_parts.append(f"Source: {ep['url']}")
        if ep.get("audio_source"):
            desc_parts.append(f"Audio: {ep['audio_source']}")
        if ep.get("word_count"):
            desc_parts.append(f"~{ep['word_count']} words")
        SubElement(item, "description").text = " | ".join(desc_parts) if desc_parts else title

        # Audio enclosure — the actual MP3 link
        audio_filename = os.path.basename(ep.get("audio_path", ""))
        audio_url = f"{config.FEED_BASE_URL}/audio/{audio_filename}"
        file_size = ep.get("file_size_bytes", 0)
        SubElement(item, "enclosure", {
            "url": audio_url,
            "length": str(file_size),
            "type": "audio/mpeg",
        })

        # Use the original URL as a unique identifier
        if ep.get("url"):
            SubElement(item, "guid", {"isPermaLink": "true"}).text = ep["url"]
        else:
            SubElement(item, "guid", {"isPermaLink": "false"}).text = audio_filename

        SubElement(item, "link").text = ep.get("url", "")

        # Publication date
        created = ep.get("created_at", "")
        if created:
            try:
                dt = datetime.fromisoformat(created)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                SubElement(item, "pubDate").text = format_datetime(dt)
            except (ValueError, TypeError):
                pass

        # iTunes episode metadata
        SubElement(item, f"{{{ITUNES_NS}}}author").text = ep.get("author", config.PODCAST_AUTHOR)
        SubElement(item, f"{{{ITUNES_NS}}}summary").text = title

        # Duration estimate (minutes → HH:MM:SS)
        minutes = ep.get("estimated_minutes", 0)
        if minutes:
            hours = int(minutes // 60)
            mins = int(minutes % 60)
            secs = int((minutes % 1) * 60)
            duration_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
            SubElement(item, f"{{{ITUNES_NS}}}duration").text = duration_str

    # Pretty-print
    indent(rss, space="  ")

    # Serialize to bytes with XML declaration
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_bytes += tostring(rss, encoding="unicode").encode("utf-8")
    return xml_bytes
