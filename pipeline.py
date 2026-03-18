"""
Pipeline orchestrator: ties together fetching, audio lookup, TTS generation,
metadata tagging, and RSS feed generation.

This is the core engine. The CLI (main.py) handles argument parsing and
user interaction, then calls into this module to do the actual work.
"""

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import config
from fetcher import fetch_url, FetchResult
from audio_lookup import check_existing_audio, AudioLookupResult
from tts_client import TTSClient
from feed_generator import regenerate_feed
from chapters import add_chapters_to_mp3

logger = logging.getLogger(__name__)

# Optional: mutagen for MP3 ID3 tagging
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, COMM, TCON
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    logger.info("mutagen not installed — MP3 metadata tagging disabled. "
                "Install with: pip install mutagen")


@dataclass
class PipelineResult:
    """Result of processing a single URL through the pipeline."""
    url: str
    title: str = ""
    author: str = ""
    audio_path: str = ""
    audio_source: str = ""  # "kokoro-tts", "lesswrong-embedded", etc.
    word_count: int = 0
    estimated_minutes: float = 0.0
    success: bool = True
    error: str = ""
    skipped: bool = False
    skip_reason: str = ""


class Pipeline:
    """Main pipeline orchestrator."""

    def __init__(self, mock_tts: bool = False):
        self.tts = TTSClient(mock=mock_tts)
        self.mock_tts = mock_tts
        self._ensure_directories()
        self._ensure_database()

    def _ensure_directories(self):
        """Create output directories if they don't exist."""
        for d in [config.OUTPUT_DIR, config.AUDIO_DIR, config.FEED_DIR]:
            os.makedirs(d, exist_ok=True)

    def _ensure_database(self):
        """Create the SQLite database and episodes table."""
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                url TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                audio_path TEXT,
                audio_source TEXT,
                word_count INTEGER,
                estimated_minutes REAL,
                file_size_bytes INTEGER,
                created_at TEXT,
                fetch_method TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()
        conn.close()

    def process_url(self, url: str, force: bool = False,
                    skip_lookup: bool = False) -> PipelineResult:
        """
        Process a single URL through the full pipeline:
          1. Check if already processed (skip if so, unless force=True)
          2. Fetch and clean article text
          3. Check for existing audio (unless skip_lookup=True)
          4. Generate TTS if no existing audio
          5. Tag MP3 with metadata
          6. Record in database

        Args:
            url: The URL to process.
            force: Re-process even if already in the database.
            skip_lookup: Skip checking for existing audio.

        Returns:
            PipelineResult with details of what happened.
        """
        url = url.strip()
        if not url:
            return PipelineResult(url=url, success=False, error="Empty URL")

        # Normalize
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # ── Step 1: Check if already processed ────────────────────
        if not force and self._is_already_processed(url):
            return PipelineResult(
                url=url, skipped=True,
                skip_reason="Already processed (use --force to re-process)"
            )

        # ── Step 2: Fetch article text ────────────────────────────
        logger.info(f"Processing: {url}")
        fetch_result = fetch_url(url)

        if not fetch_result.success:
            self._record_failure(url, fetch_result.error)
            return PipelineResult(
                url=url, title=fetch_result.title,
                success=False, error=f"Fetch failed: {fetch_result.error}"
            )

        if not fetch_result.audio_text:
            self._record_failure(url, "No text extracted")
            return PipelineResult(
                url=url, title=fetch_result.title,
                success=False, error="No usable text extracted"
            )

        title = fetch_result.title or _title_from_url(url)
        author = fetch_result.author

        # ── Step 3: Check for existing audio ──────────────────────
        audio_path = ""
        audio_source = ""

        if not skip_lookup:
            lookup = check_existing_audio(url)
            if lookup.audio_found:
                # Download the existing audio
                audio_path = self._download_audio(
                    lookup.audio_url, title
                )
                if audio_path:
                    audio_source = lookup.audio_source
                    logger.info(f"  Using existing audio from {audio_source}")

        # ── Step 4: Generate TTS if needed ────────────────────────
        if not audio_path:
            audio_filename = _safe_filename(title) + ".mp3"
            audio_path = os.path.join(config.AUDIO_DIR, audio_filename)

            logger.info(f"  Generating TTS: {fetch_result.word_count} words, "
                        f"~{fetch_result.estimated_minutes} min...")

            success = self.tts.synthesize_long_text(
                fetch_result.audio_text, audio_path
            )
            if not success:
                self._record_failure(url, "TTS generation failed")
                return PipelineResult(
                    url=url, title=title, author=author,
                    success=False, error="TTS generation failed"
                )
            audio_source = "kokoro-tts"

        # ── Step 5: Tag MP3 with metadata & chapters ────────────
        if audio_path and os.path.exists(audio_path):
            _tag_mp3(audio_path, title=title, author=author, url=url)

            # Embed chapter markers from headings in the raw text
            num_chapters = add_chapters_to_mp3(
                audio_path, fetch_result.text, title=title
            )
            if num_chapters:
                logger.info(f"  Added {num_chapters} chapters")

        # ── Step 6: Record in database ────────────────────────────
        file_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0

        self._record_success(
            url=url, title=title, author=author,
            audio_path=audio_path, audio_source=audio_source,
            word_count=fetch_result.word_count,
            estimated_minutes=fetch_result.estimated_minutes,
            file_size=file_size,
            fetch_method=fetch_result.extraction_method,
        )

        return PipelineResult(
            url=url, title=title, author=author,
            audio_path=audio_path, audio_source=audio_source,
            word_count=fetch_result.word_count,
            estimated_minutes=fetch_result.estimated_minutes,
            success=True,
        )

    def process_urls(self, urls: list[str], force: bool = False,
                     skip_lookup: bool = False,
                     progress_callback=None) -> list[PipelineResult]:
        """Process multiple URLs and regenerate the feed."""
        results = []
        total = len(urls)

        for i, url in enumerate(urls):
            result = self.process_url(url, force=force,
                                      skip_lookup=skip_lookup)
            results.append(result)

            status = "✓" if result.success else ("⊘" if result.skipped else "✗")
            logger.info(f"[{i+1}/{total}] {status} {result.title or url}")

            if progress_callback:
                progress_callback(i + 1, total, result)

        # Regenerate the podcast feed
        successful = sum(1 for r in results if r.success)
        if successful > 0:
            logger.info(f"\nRegenerating podcast feed...")
            regenerate_feed()
            logger.info(f"Feed updated. Subscribe in your podcast app at: "
                        f"{config.FEED_BASE_URL}/feed/feed.xml")

        return results

    def _is_already_processed(self, url: str) -> bool:
        """Check if a URL has already been successfully processed."""
        conn = sqlite3.connect(config.DB_PATH)
        try:
            row = conn.execute(
                "SELECT status FROM episodes WHERE url = ?", (url,)
            ).fetchone()
            return row is not None and row[0] == "complete"
        finally:
            conn.close()

    def _record_success(self, url: str, title: str, author: str,
                        audio_path: str, audio_source: str,
                        word_count: int, estimated_minutes: float,
                        file_size: int, fetch_method: str):
        """Record a successfully processed episode in the database."""
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO episodes
                    (url, title, author, audio_path, audio_source,
                     word_count, estimated_minutes, file_size_bytes,
                     created_at, fetch_method, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            """, (url, title, author, audio_path, audio_source,
                  word_count, estimated_minutes, file_size,
                  datetime.now(timezone.utc).isoformat(), fetch_method))
            conn.commit()
        finally:
            conn.close()

    def _record_failure(self, url: str, error: str):
        """Record a failed processing attempt."""
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO episodes
                    (url, title, status, created_at)
                VALUES (?, ?, 'failed', ?)
            """, (url, error, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()

    def _download_audio(self, audio_url: str, title: str) -> str:
        """Download an existing audio file."""
        try:
            resp = requests.get(audio_url, timeout=60, stream=True)
            resp.raise_for_status()

            # Determine extension from content-type or URL
            ext = ".mp3"
            ct = resp.headers.get("content-type", "")
            if "ogg" in ct:
                ext = ".ogg"
            elif "m4a" in ct or "mp4" in ct:
                ext = ".m4a"

            filename = _safe_filename(title) + ext
            output_path = os.path.join(config.AUDIO_DIR, filename)
            os.makedirs(config.AUDIO_DIR, exist_ok=True)

            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"  Downloaded existing audio → {output_path}")
            return output_path

        except Exception as e:
            logger.warning(f"  Failed to download existing audio: {e}")
            return ""


# ── Utility functions ──────────────────────────────────────────────────

def _safe_filename(title: str) -> str:
    """Convert a title to a safe filename."""
    # Replace problematic characters
    safe = re.sub(r'[^\w\s\-]', '', title)
    safe = re.sub(r'\s+', '_', safe.strip())
    # Truncate to reasonable length
    if len(safe) > 100:
        safe = safe[:100]
    # Fallback
    if not safe:
        safe = f"article_{int(time.time())}"
    return safe


def _tag_mp3(path: str, title: str = "", author: str = "", url: str = ""):
    """Add ID3 metadata tags to an MP3 file."""
    if not HAS_MUTAGEN:
        return

    try:
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()

        tags = audio.tags
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if author:
            tags.add(TPE1(encoding=3, text=author))
        tags.add(TALB(encoding=3, text=config.PODCAST_TITLE))
        tags.add(TCON(encoding=3, text="Podcast"))
        if url:
            tags.add(COMM(encoding=3, lang="eng", desc="source",
                          text=url))
        audio.save()
        logger.debug(f"  Tagged MP3: {title}")
    except Exception as e:
        logger.debug(f"  MP3 tagging failed (non-critical): {e}")


import requests  # already imported above, but making the download dep clear


def _title_from_url(url: str) -> str:
    """Generate a title from a URL when none is available."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path:
        # Use the last path segment, cleaned up
        segment = path.split("/")[-1]
        segment = segment.replace("-", " ").replace("_", " ")
        # Remove file extensions
        segment = re.sub(r'\.\w+$', '', segment)
        return segment.title()
    return parsed.netloc
