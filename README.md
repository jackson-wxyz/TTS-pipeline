# TTS Audio Pipeline

Converts articles, blog posts, and essays into podcast-quality audio using local text-to-speech, then serves them as a personal podcast feed you can subscribe to in Pocket Casts (or any podcast app).

## Architecture

```
URL arrives (from triage pipeline, CLI, text file, etc.)
    ↓
Fetch article text (trafilatura), clean for audio
    ↓
Check for existing audio:
  - LessWrong/EA Forum → check for embedded audio on post page
  - Other URLs → skip to generation
    ↓
If existing audio found → download MP3
If not → send text to Kokoro-FastAPI → receive MP3
    ↓
Tag MP3 with metadata (title, author, source)
    ↓
Record in SQLite, regenerate RSS feed
    ↓
Pocket Casts picks it up on next refresh
```

## Setup

### 1. Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Kokoro-FastAPI (TTS server)

Kokoro is a lightweight 82M-parameter TTS model that runs at ~90x real-time on consumer GPUs. Kokoro-FastAPI wraps it in an OpenAI-compatible API server.

```bash
# On your gaming desktop (needs Docker + NVIDIA GPU):
docker run -d  --gpus all -p 8880:8880   --name kokoro-tts kokoro-blackwell
```

Verify it's running:
```bash
curl http://localhost:8880/v1/models
```

If your TTS server is on a different machine, update `KOKORO_BASE_URL` in `config.py`.

### 3. Configure

Edit `config.py` to set:
- `KOKORO_BASE_URL` — your Kokoro server address
- `KOKORO_VOICE` — default voice (run `python main.py --list-voices` to see options)
- `FEED_BASE_URL` — where the feed will be served from (your LAN IP for local use)
- `PODCAST_TITLE` — whatever you want your podcast called

## Usage

### Process a single URL
```bash
python main.py https://slatestarcodex.com/2014/07/30/meditations-on-moloch/
```

### Process a file of URLs
```bash
python main.py --file TTS_test_urls.txt
```

The URL file format is simple — one URL per line, `#` for comments:
```
# My reading queue
https://example.com/article-1
https://example.com/article-2  # interesting post about X
```

### Start the podcast feed server
```bash
python main.py --serve
```

Then add `http://<your-ip>:8888/feed/feed.xml` to Pocket Casts. `http://192.168.10.75:1234:8888/feed/feed.xml`

### Other commands
```bash
python main.py --health        # check Kokoro connection
python main.py --list-voices   # list available TTS voices
python main.py --feed-only     # just regenerate the RSS feed
python main.py --mock          # test without Kokoro running
python main.py --force         # re-process already-completed URLs
python main.py --skip-lookup   # skip checking for existing audio
```

## Project Structure

```
TTS-pipeline/
├── main.py              # CLI entry point
├── config.py            # All configuration in one place
├── fetcher.py           # URL fetching + audio-optimized text cleaning
├── audio_lookup.py      # Check for existing audio (LW, EAF, etc.)
├── tts_client.py        # Kokoro-FastAPI client
├── feed_generator.py    # RSS podcast feed generation
├── pipeline.py          # Orchestrator (ties everything together)
├── requirements.txt
├── output/
│   ├── audio/           # Generated MP3 files
│   ├── feed/            # RSS feed XML
│   └── pipeline.db      # SQLite database tracking all episodes
```

## How it fits into the larger system

This is "Project 4" in the broader content-optimization plan. It currently accepts URLs from:
- Text files (manual curation or output from other pipelines)
- Command-line arguments (one-offs)

Future integrations:
- Tab-triage pipeline (Project 1) → high-interest articles auto-queued for audio
- AI digest newsletters (Project 3) → top articles auto-narrated
- Read-later app (Project 2) → "send to audio" action
