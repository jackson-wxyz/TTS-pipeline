"""
EPUB handler: converts EPUB files into audiobooks.

Reads an EPUB file, extracts chapters, cleans text for TTS,
generates audio per-chapter via Kokoro-FastAPI, tags each file
with chapter metadata, and optionally produces a single
concatenated MP3 with embedded chapter markers.

Dependencies:
    pip install ebooklib beautifulsoup4

Output modes:
  - "chapters": One MP3 per chapter (simpler, good for podcast feeds)
  - "single":   One big MP3 with embedded ID3 chapter markers
  - "both":     Both outputs (default)
"""

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Optional: ebooklib for EPUB parsing
try:
    import ebooklib
    from ebooklib import epub
    HAS_EBOOKLIB = True
except ImportError:
    HAS_EBOOKLIB = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

from fetcher import clean_text_for_audio
from chapters import Chapter, compute_chapter_timestamps, embed_chapters_in_mp3


@dataclass
class EpubChapter:
    """A chapter extracted from an EPUB file."""
    title: str
    raw_html: str = ""
    raw_text: str = ""
    audio_text: str = ""     # cleaned for TTS
    word_count: int = 0
    index: int = 0


@dataclass
class EpubBook:
    """Container for a parsed EPUB file."""
    title: str = ""
    author: str = ""
    language: str = ""
    chapters: list = field(default_factory=list)  # list[EpubChapter]
    cover_image: bytes = b""
    total_words: int = 0
    estimated_minutes: float = 0.0


def parse_epub(epub_path: str) -> EpubBook:
    """
    Parse an EPUB file and extract chapters with clean text.

    Args:
        epub_path: Path to the .epub file.

    Returns:
        EpubBook with chapters extracted and text cleaned for TTS.
    """
    if not HAS_EBOOKLIB:
        raise ImportError(
            "ebooklib is required for EPUB support. "
            "Install with: pip install ebooklib"
        )
    if not HAS_BS4:
        raise ImportError(
            "beautifulsoup4 is required for EPUB support. "
            "Install with: pip install beautifulsoup4"
        )

    logger.info(f"Parsing EPUB: {epub_path}")
    book = epub.read_epub(epub_path, options={"ignore_ncx": False})

    # Extract metadata
    result = EpubBook()
    result.title = _get_metadata(book, "title") or os.path.basename(epub_path)
    result.author = _get_metadata(book, "creator") or ""
    result.language = _get_metadata(book, "language") or "en"

    logger.info(f"  Title: {result.title}")
    logger.info(f"  Author: {result.author}")

    # Extract cover image
    result.cover_image = _extract_cover(book)

    # Extract chapters from the spine (reading order)
    chapter_index = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        # Skip non-content items (table of contents, colophon, etc.)
        content = item.get_content().decode("utf-8", errors="replace")

        if not content or len(content) < 100:
            continue

        # Extract text from HTML
        soup = BeautifulSoup(content, "html.parser")

        # Try to find a chapter title
        title = _extract_chapter_title(soup, chapter_index)

        # Get clean text
        raw_text = soup.get_text(separator="\n", strip=True)

        # Skip very short "chapters" (likely front matter, copyright, etc.)
        if len(raw_text.split()) < 50:
            continue

        # Clean for audio
        audio_text = clean_text_for_audio(raw_text, title=title)

        chapter = EpubChapter(
            title=title,
            raw_html=content,
            raw_text=raw_text,
            audio_text=audio_text,
            word_count=len(audio_text.split()),
            index=chapter_index,
        )
        result.chapters.append(chapter)
        chapter_index += 1

    result.total_words = sum(ch.word_count for ch in result.chapters)
    result.estimated_minutes = round(result.total_words / 150, 1)

    logger.info(f"  Chapters: {len(result.chapters)}")
    logger.info(f"  Total words: {result.total_words:,}")
    logger.info(f"  Estimated duration: ~{result.estimated_minutes} minutes")

    return result


def generate_epub_audio(epub_path: str, output_dir: str,
                        tts_client, mode: str = "both") -> dict:
    """
    Generate audio from an EPUB file.

    Args:
        epub_path: Path to the .epub file.
        output_dir: Directory for audio output.
        tts_client: A TTSClient instance (from tts_client.py).
        mode: "chapters" (one MP3 per chapter), "single" (one big MP3
              with chapter markers), or "both".

    Returns:
        Dict with keys:
          - book: EpubBook metadata
          - chapter_files: list of per-chapter MP3 paths
          - combined_file: path to combined MP3 (if mode includes "single")
          - success: bool
          - errors: list of error strings
    """
    book = parse_epub(epub_path)

    if not book.chapters:
        return {
            "book": book, "chapter_files": [], "combined_file": "",
            "success": False, "errors": ["No chapters found in EPUB"]
        }

    # Create output directory named after the book
    safe_title = re.sub(r'[^\w\s\-]', '', book.title)
    safe_title = re.sub(r'\s+', '_', safe_title.strip())[:80]
    book_dir = os.path.join(output_dir, safe_title or "epub_book")
    os.makedirs(book_dir, exist_ok=True)

    chapter_files = []
    errors = []

    # ── Generate audio per chapter ────────────────────────────────
    logger.info(f"\nGenerating audio for {len(book.chapters)} chapters...")

    for i, chapter in enumerate(book.chapters):
        chapter_filename = f"{i+1:03d}_{_safe_fn(chapter.title)}.mp3"
        chapter_path = os.path.join(book_dir, chapter_filename)

        logger.info(f"  [{i+1}/{len(book.chapters)}] {chapter.title} "
                     f"({chapter.word_count} words)")

        success = tts_client.synthesize_long_text(
            chapter.audio_text, chapter_path
        )

        if success:
            # Tag the chapter MP3
            _tag_chapter_mp3(
                chapter_path, chapter, book, i + 1, len(book.chapters)
            )
            chapter_files.append(chapter_path)
        else:
            errors.append(f"TTS failed for chapter {i+1}: {chapter.title}")
            logger.warning(f"    TTS failed for chapter {i+1}")

    # ── Optionally combine into single file with chapters ─────────
    combined_file = ""
    if mode in ("single", "both") and chapter_files:
        combined_file = os.path.join(
            book_dir, f"{safe_title}_complete.mp3"
        )
        logger.info(f"\nCombining {len(chapter_files)} chapters into "
                     f"single file...")

        success = _concatenate_mp3s(chapter_files, combined_file)
        if success:
            # Embed chapter markers
            _embed_epub_chapters(combined_file, book, chapter_files)
            logger.info(f"  Combined audiobook: {combined_file}")
        else:
            errors.append("Failed to concatenate chapter files")
            combined_file = ""

    # ── Clean up per-chapter files if only "single" mode ──────────
    if mode == "single" and combined_file and chapter_files:
        for f in chapter_files:
            try:
                os.remove(f)
            except OSError:
                pass
        chapter_files = []

    return {
        "book": book,
        "chapter_files": chapter_files,
        "combined_file": combined_file,
        "success": len(errors) == 0 or len(chapter_files) > 0,
        "errors": errors,
    }


# ── Internal helpers ──────────────────────────────────────────────────

def _get_metadata(book, field: str) -> str:
    """Extract a metadata field from an EPUB."""
    try:
        values = book.get_metadata("DC", field)
        if values:
            return values[0][0]
    except Exception:
        pass
    return ""


def _extract_cover(book) -> bytes:
    """Try to extract the cover image from an EPUB."""
    try:
        # Method 1: Look for item with "cover" in ID
        for item in book.get_items():
            if "cover" in (item.get_id() or "").lower():
                if item.get_type() in (ebooklib.ITEM_IMAGE, ebooklib.ITEM_COVER):
                    return item.get_content()

        # Method 2: Look for cover-image metadata
        cover_meta = book.get_metadata("OPF", "cover")
        if cover_meta:
            cover_id = cover_meta[0][1].get("content", "")
            if cover_id:
                for item in book.get_items():
                    if item.get_id() == cover_id:
                        return item.get_content()
    except Exception:
        pass
    return b""


def _extract_chapter_title(soup, index: int) -> str:
    """Extract the chapter title from HTML content."""
    # Look for headings
    for tag in ["h1", "h2", "h3", "title"]:
        el = soup.find(tag)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) < 200:
                return text

    # Look for elements with class containing "title" or "chapter"
    for el in soup.find_all(class_=re.compile(r"title|chapter|heading",
                                                re.IGNORECASE)):
        text = el.get_text(strip=True)
        if text and len(text) < 200:
            return text

    return f"Chapter {index + 1}"


def _safe_fn(text: str) -> str:
    """Convert text to a safe filename fragment."""
    safe = re.sub(r'[^\w\s\-]', '', text)
    safe = re.sub(r'\s+', '_', safe.strip())
    return safe[:60] or "untitled"


def _tag_chapter_mp3(path: str, chapter: EpubChapter,
                     book: EpubBook, track_num: int, total_tracks: int):
    """Tag an individual chapter MP3 with metadata."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import (
            ID3, TIT2, TPE1, TALB, TRCK, TCON
        )

        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()

        tags = audio.tags
        tags.add(TIT2(encoding=3, text=[chapter.title]))
        if book.author:
            tags.add(TPE1(encoding=3, text=[book.author]))
        tags.add(TALB(encoding=3, text=[book.title]))
        tags.add(TRCK(encoding=3, text=[f"{track_num}/{total_tracks}"]))
        tags.add(TCON(encoding=3, text=["Audiobook"]))
        audio.save()
    except Exception as e:
        logger.debug(f"  Chapter tagging failed: {e}")


def _concatenate_mp3s(input_files: list[str], output_path: str) -> bool:
    """
    Concatenate multiple MP3 files into a single file.

    Uses simple binary concatenation — works for MP3s with the same
    encoding parameters (which they will be, since they all come from
    the same Kokoro voice).
    """
    try:
        with open(output_path, "wb") as out:
            for f in input_files:
                with open(f, "rb") as inp:
                    out.write(inp.read())
        return True
    except Exception as e:
        logger.warning(f"  MP3 concatenation failed: {e}")
        return False


def _embed_epub_chapters(combined_path: str, book: EpubBook,
                         chapter_files: list[str]):
    """Embed chapter markers in the combined MP3 from per-chapter durations."""
    try:
        from mutagen.mp3 import MP3

        # Get the actual duration of each chapter file
        chapter_durations_ms = []
        for f in chapter_files:
            audio = MP3(f)
            chapter_durations_ms.append(int(audio.info.length * 1000))

        # Build Chapter objects with real timestamps
        chapters = []
        cumulative_ms = 0
        for i, (ch, dur_ms) in enumerate(zip(book.chapters, chapter_durations_ms)):
            chapters.append(Chapter(
                title=ch.title,
                level=1,
                word_offset=0,
                start_ms=cumulative_ms,
                end_ms=cumulative_ms + dur_ms,
            ))
            cumulative_ms += dur_ms

        # Embed using the chapters module
        embed_chapters_in_mp3(combined_path, chapters)

        # Also add book-level metadata
        from mutagen.id3 import TIT2, TPE1, TALB, TCON
        audio = MP3(combined_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags.add(TIT2(encoding=3, text=[book.title]))
        if book.author:
            tags.add(TPE1(encoding=3, text=[book.author]))
        tags.add(TALB(encoding=3, text=[book.title]))
        tags.add(TCON(encoding=3, text=["Audiobook"]))
        audio.save()

    except Exception as e:
        logger.warning(f"  Chapter embedding failed: {e}")
