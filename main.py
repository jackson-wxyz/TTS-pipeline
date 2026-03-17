#!/usr/bin/env python3
"""
TTS Audio Pipeline — CLI entry point.

Converts articles and blog posts into podcast-quality audio using
local TTS (Kokoro-FastAPI) and serves them as a personal podcast feed.

Usage:
    # Process a single URL
    python main.py https://slatestarcodex.com/2014/07/30/meditations-on-moloch/

    # Process a file of URLs (one per line)
    python main.py --file urls.txt

    # Force re-processing of already-completed URLs
    python main.py --file urls.txt --force

    # Skip audio lookup (always generate fresh TTS)
    python main.py --file urls.txt --skip-lookup

    # Use mock TTS (for testing without Kokoro running)
    python main.py --file urls.txt --mock

    # Just regenerate the RSS feed from existing episodes
    python main.py --feed-only

    # Start the feed server (serves RSS + audio files)
    python main.py --serve

    # List available Kokoro voices
    python main.py --list-voices

    # Check pipeline health (Kokoro connection, etc.)
    python main.py --health
"""

import argparse
import http.server
import logging
import os
import sys
import threading

import config
from pipeline import Pipeline
from feed_generator import regenerate_feed

# ── Logging setup ─────────────────────────────────────────────────────

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("trafilatura").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ── URL loading ───────────────────────────────────────────────────────

def load_urls_from_file(path: str) -> list[str]:
    """
    Load URLs from a text file. Supports:
      - One URL per line
      - Lines starting with # are comments
      - Blank lines are skipped
      - Inline comments after URLs (separated by whitespace + #)
    """
    urls = []
    with open(path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip inline comments
            if " #" in line:
                line = line[:line.index(" #")].strip()
            if "\t#" in line:
                line = line[:line.index("\t#")].strip()
            if line:
                urls.append(line)
    return urls


# ── Feed server ───────────────────────────────────────────────────────

def start_feed_server(port: int = None):
    """
    Start a simple HTTP server that serves the podcast feed and audio files.

    Directory structure served:
      /feed/feed.xml  → the RSS feed (subscribe to this in Pocket Casts)
      /audio/*.mp3    → the audio files
    """
    port = port or config.FEED_SERVER_PORT
    serve_dir = config.OUTPUT_DIR

    if not os.path.exists(serve_dir):
        print(f"Output directory not found: {serve_dir}")
        print("Run the pipeline first to generate some audio.")
        sys.exit(1)

    os.chdir(serve_dir)

    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("0.0.0.0", port), handler)

    print(f"\n{'='*60}")
    print(f"  Podcast Feed Server")
    print(f"{'='*60}")
    print(f"  Feed URL:  http://localhost:{port}/feed/feed.xml")
    print(f"  Audio dir: {config.AUDIO_DIR}")
    print(f"")
    print(f"  Add the feed URL to Pocket Casts (or any podcast app)")
    print(f"  to subscribe to your reading queue.")
    print(f"")
    print(f"  For access from other devices on your network:")
    print(f"  → Find your LAN IP and use http://<ip>:{port}/feed/feed.xml")
    print(f"{'='*60}")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.shutdown()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TTS Audio Pipeline — convert articles to podcast audio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input sources
    parser.add_argument(
        "urls", nargs="*",
        help="URLs to process (can also use --file)"
    )
    parser.add_argument(
        "-f", "--file",
        help="Text file containing URLs (one per line)"
    )

    # Processing options
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process URLs even if already completed"
    )
    parser.add_argument(
        "--skip-lookup", action="store_true",
        help="Skip checking for existing audio (always generate TTS)"
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Use mock TTS (for testing without Kokoro running)"
    )
    parser.add_argument(
        "--voice",
        help="Kokoro voice to use (default: from config)"
    )

    # Utility commands
    parser.add_argument(
        "--feed-only", action="store_true",
        help="Just regenerate the RSS feed (no processing)"
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="Start the HTTP server to serve the podcast feed"
    )
    parser.add_argument(
        "--list-voices", action="store_true",
        help="List available Kokoro TTS voices"
    )
    parser.add_argument(
        "--health", action="store_true",
        help="Check pipeline health (Kokoro connection, etc.)"
    )

    # Output options
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--port", type=int,
        help=f"Port for feed server (default: {config.FEED_SERVER_PORT})"
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # ── Utility commands ──────────────────────────────────────────
    if args.health:
        _cmd_health(args)
        return

    if args.list_voices:
        _cmd_list_voices(args)
        return

    if args.feed_only:
        _cmd_feed_only()
        return

    if args.serve:
        start_feed_server(args.port)
        return

    # ── Gather URLs ───────────────────────────────────────────────
    urls = list(args.urls)
    if args.file:
        file_urls = load_urls_from_file(args.file)
        logging.info(f"Loaded {len(file_urls)} URLs from {args.file}")
        urls.extend(file_urls)

    if not urls:
        parser.print_help()
        print("\nError: No URLs provided. Pass URLs as arguments or use --file.")
        sys.exit(1)

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for u in urls:
        normalized = u.strip().rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            unique_urls.append(u.strip())
    urls = unique_urls

    # ── Run the pipeline ──────────────────────────────────────────
    pipeline = Pipeline(mock_tts=args.mock)

    if args.voice:
        pipeline.tts.voice = args.voice

    print(f"\nProcessing {len(urls)} URL(s)...")
    if args.mock:
        print("  (using mock TTS — no Kokoro needed)")
    print()

    results = pipeline.process_urls(
        urls,
        force=args.force,
        skip_lookup=args.skip_lookup,
    )

    # ── Summary ───────────────────────────────────────────────────
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success and not r.skipped]
    skipped = [r for r in results if r.skipped]

    print(f"\n{'='*60}")
    print(f"  Pipeline Complete")
    print(f"{'='*60}")
    print(f"  Processed: {len(results)}")
    print(f"  Success:   {len(successful)}")
    print(f"  Failed:    {len(failed)}")
    print(f"  Skipped:   {len(skipped)}")

    if successful:
        total_words = sum(r.word_count for r in successful)
        total_mins = sum(r.estimated_minutes for r in successful)
        print(f"\n  Total words: {total_words:,}")
        print(f"  Total audio: ~{total_mins:.0f} minutes")

    if failed:
        print(f"\n  Failed URLs:")
        for r in failed:
            print(f"    ✗ {r.url}")
            print(f"      {r.error}")

    print(f"\n  Feed URL: {config.FEED_BASE_URL}/feed/feed.xml")
    print(f"  Run 'python main.py --serve' to start the feed server")
    print(f"{'='*60}\n")


def _cmd_health(args):
    """Check pipeline health."""
    from tts_client import TTSClient

    print(f"\nPipeline Health Check")
    print(f"{'='*40}")

    # Check output directories
    for name, path in [("Output", config.OUTPUT_DIR),
                        ("Audio", config.AUDIO_DIR),
                        ("Feed", config.FEED_DIR)]:
        exists = os.path.exists(path)
        print(f"  {name} dir: {'✓' if exists else '✗'} {path}")

    # Check database
    db_exists = os.path.exists(config.DB_PATH)
    print(f"  Database:  {'✓' if db_exists else '—'} {config.DB_PATH}")

    if db_exists:
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE status = 'complete'"
            ).fetchone()[0]
            print(f"  Episodes:  {count} completed")
        except Exception:
            print(f"  Episodes:  (table not yet created)")
        finally:
            conn.close()

    # Check Kokoro-FastAPI
    print()
    tts = TTSClient(mock=args.mock)
    if args.mock:
        print(f"  Kokoro:    ✓ (mock mode)")
    else:
        healthy = tts.health_check()
        print(f"  Kokoro:    {'✓ connected' if healthy else '✗ not reachable'}")
        print(f"             {config.KOKORO_BASE_URL}")
        if not healthy:
            print(f"\n  To start Kokoro-FastAPI:")
            print(f"    docker run -d --gpus all -p 8880:8880 "
                  f"ghcr.io/remsky/kokoro-fastapi:latest")

    print()


def _cmd_list_voices(args):
    """List available Kokoro voices."""
    from tts_client import TTSClient
    tts = TTSClient(mock=args.mock)
    voices = tts.list_voices()
    if voices:
        print(f"\nAvailable Kokoro voices ({len(voices)}):")
        for v in sorted(voices):
            marker = " ← current" if v == config.KOKORO_VOICE else ""
            print(f"  {v}{marker}")
    else:
        print("\nCould not retrieve voice list.")
        print("Is Kokoro-FastAPI running?")
    print()


def _cmd_feed_only():
    """Regenerate the RSS feed."""
    feed_path = regenerate_feed()
    print(f"\nFeed regenerated: {feed_path}")
    print(f"Subscribe at: {config.FEED_BASE_URL}/feed/feed.xml")


if __name__ == "__main__":
    main()
