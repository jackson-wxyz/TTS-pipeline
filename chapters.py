"""
Chapter extraction and MP3 chapter marker embedding.

Extracts headings from article text (markdown or plain), estimates
time positions based on word-count proportions, and writes ID3v2
CHAP/CTOC frames so podcast apps can display chapter navigation.

Supported by: Pocket Casts, Apple Podcasts, Overcast, Podcast Addict,
and most modern podcast apps.

Chapter format: ID3v2 CHAP frames (the podcast standard).
  - CTOC: Table of Contents frame (lists all chapters)
  - CHAP: Individual chapter with start/end time + title

Time estimation approach:
  Since we don't have the actual audio waveform at heading-extraction
  time, we estimate chapter timestamps based on the proportion of words
  before each heading. This is surprisingly accurate for TTS audio
  because TTS speaks at a very consistent rate (no pauses for thought,
  laughter, etc.). We then adjust using the actual MP3 duration once
  the file is generated.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Optional mutagen imports
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, CTOC, CHAP, TIT2, CTOCFlags,
    )
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False


@dataclass
class Chapter:
    """A chapter extracted from article text."""
    title: str
    level: int              # heading level (1 = h1, 2 = h2, etc.)
    word_offset: int        # word count from start of text to this heading
    start_ms: int = 0       # millisecond offset (filled in after TTS)
    end_ms: int = 0         # millisecond offset (filled in after TTS)


def extract_chapters(text: str, title: str = "",
                     min_level: int = 1, max_level: int = 3) -> list[Chapter]:
    """
    Extract chapter boundaries from article text.

    Looks for markdown headings (## Heading), HTML headings (<h2>),
    and all-caps section headers. Returns a list of Chapter objects
    with word offsets that can be converted to timestamps.

    Args:
        text: The raw (pre-cleaning) article text.
        title: Article title (used as the first "Introduction" chapter).
        min_level: Minimum heading level to include (1 = h1).
        max_level: Maximum heading level to include (3 = h3).

    Returns:
        List of Chapter objects. Empty list if fewer than 2 headings found.
    """
    chapters = []
    words_so_far = 0

    # Add an "Introduction" chapter for the start of the article
    if title:
        chapters.append(Chapter(
            title=title, level=0, word_offset=0
        ))

    lines = text.splitlines()

    for line in lines:
        stripped = line.strip()

        # Count words in this line (for tracking position)
        line_words = len(stripped.split()) if stripped else 0

        # ── Markdown headings: ## Heading ──────────────────────
        md_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if md_match:
            level = len(md_match.group(1))
            heading_text = md_match.group(2).strip()
            # Strip trailing # markers (e.g., "## Heading ##")
            heading_text = re.sub(r'\s*#+\s*$', '', heading_text)

            if min_level <= level <= max_level and heading_text:
                chapters.append(Chapter(
                    title=heading_text,
                    level=level,
                    word_offset=words_so_far,
                ))

        # ── HTML headings: <h2>Heading</h2> ───────────────────
        html_match = re.match(
            r'<h([1-6])[^>]*>(.*?)</h\1>',
            stripped, re.IGNORECASE | re.DOTALL
        )
        if html_match:
            level = int(html_match.group(1))
            heading_text = re.sub(r'<[^>]+>', '', html_match.group(2)).strip()

            if min_level <= level <= max_level and heading_text:
                chapters.append(Chapter(
                    title=heading_text,
                    level=level,
                    word_offset=words_so_far,
                ))

        words_so_far += line_words

    # If we only have the intro chapter (or nothing), not worth it
    if len(chapters) < 2:
        return []

    # Remove duplicate chapters at the same word offset
    seen_offsets = set()
    unique = []
    for ch in chapters:
        if ch.word_offset not in seen_offsets:
            seen_offsets.add(ch.word_offset)
            unique.append(ch)
    chapters = unique

    # Store total word count for timestamp calculation
    for ch in chapters:
        ch._total_words = words_so_far

    return chapters


def compute_chapter_timestamps(chapters: list[Chapter],
                               duration_ms: int,
                               total_words: int = 0) -> list[Chapter]:
    """
    Convert word offsets to millisecond timestamps.

    Uses proportional word-count mapping: if a heading is at word 500
    out of 2000 total words, its timestamp is at 25% of the total
    duration. This works well for TTS since it speaks at a constant rate.

    Args:
        chapters: List of chapters with word_offset set.
        duration_ms: Total audio duration in milliseconds.
        total_words: Total word count (if 0, uses the max word_offset).

    Returns:
        Same list with start_ms and end_ms populated.
    """
    if not chapters or duration_ms <= 0:
        return chapters

    if total_words <= 0:
        total_words = max(ch.word_offset for ch in chapters) or 1

    for i, ch in enumerate(chapters):
        # Start time: proportional to word offset
        proportion = ch.word_offset / total_words if total_words > 0 else 0
        ch.start_ms = int(proportion * duration_ms)

        # End time: start of next chapter (or end of audio for last chapter)
        if i + 1 < len(chapters):
            next_proportion = chapters[i + 1].word_offset / total_words
            ch.end_ms = int(next_proportion * duration_ms)
        else:
            ch.end_ms = duration_ms

    return chapters


def embed_chapters_in_mp3(mp3_path: str, chapters: list[Chapter]) -> bool:
    """
    Write ID3v2 CHAP and CTOC frames to an MP3 file.

    This gives podcast apps chapter navigation: a list of titled
    sections with timestamps that users can tap to jump to.

    Args:
        mp3_path: Path to the MP3 file.
        chapters: List of chapters with start_ms and end_ms set.

    Returns:
        True if chapters were written, False otherwise.
    """
    if not HAS_MUTAGEN:
        logger.debug("mutagen not available — skipping chapter embedding")
        return False

    if not chapters:
        return False

    try:
        audio = MP3(mp3_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        # Get actual duration from the MP3 for the last chapter's end
        duration_ms = int(audio.info.length * 1000)

        # Ensure last chapter ends at actual duration
        if chapters:
            chapters[-1].end_ms = duration_ms

        # Build element IDs for each chapter
        element_ids = [f"chp{i}" for i in range(len(chapters))]

        # Add Table of Contents (CTOC) frame
        tags.add(CTOC(
            element_id="toc",
            flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
            child_element_ids=element_ids,
            sub_frames=[
                TIT2(encoding=3, text=["Table of Contents"]),
            ],
        ))

        # Add individual CHAP frames
        for i, (ch, eid) in enumerate(zip(chapters, element_ids)):
            tags.add(CHAP(
                element_id=eid,
                start_time=max(0, ch.start_ms),
                end_time=min(ch.end_ms, duration_ms),
                start_offset=0xFFFFFFFF,  # not used (byte offsets)
                end_offset=0xFFFFFFFF,
                sub_frames=[
                    TIT2(encoding=3, text=[ch.title]),
                ],
            ))

        audio.save()
        logger.info(f"  Embedded {len(chapters)} chapters in MP3")
        return True

    except Exception as e:
        logger.warning(f"  Failed to embed chapters: {e}")
        return False


def add_chapters_to_mp3(mp3_path: str, raw_text: str,
                        title: str = "") -> int:
    """
    High-level convenience function: extract chapters from text,
    compute timestamps from the MP3 duration, and embed them.

    Args:
        mp3_path: Path to the generated MP3 file.
        raw_text: The raw (pre-cleaning) article text.
        title: Article title (used for the first chapter).

    Returns:
        Number of chapters embedded (0 if none or failed).
    """
    if not HAS_MUTAGEN:
        return 0

    # Extract headings from the raw text
    chapters = extract_chapters(raw_text, title=title)
    if not chapters:
        logger.debug("  No chapters found in article text")
        return 0

    # Get MP3 duration
    try:
        audio = MP3(mp3_path)
        duration_ms = int(audio.info.length * 1000)
    except Exception as e:
        logger.warning(f"  Could not read MP3 duration: {e}")
        return 0

    # Total words for proportional mapping
    total_words = len(raw_text.split())

    # Compute timestamps
    compute_chapter_timestamps(chapters, duration_ms, total_words)

    # Embed in MP3
    if embed_chapters_in_mp3(mp3_path, chapters):
        return len(chapters)
    return 0
