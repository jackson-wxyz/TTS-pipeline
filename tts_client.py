"""
TTS Client module: generates audio via Kokoro-FastAPI.

Kokoro-FastAPI wraps the Kokoro TTS model (82M params) in an
OpenAI-compatible API server. It runs via Docker with GPU support
and exposes a /v1/audio/speech endpoint at localhost:8880.

Setup (on your gaming desktop):
    docker run -d --gpus all -p 8880:8880 ghcr.io/remsky/kokoro-fastapi:latest

The client just makes HTTP POST requests — no PyTorch needed locally.
"""

import io
import logging
import os
import time

import requests

import config

logger = logging.getLogger(__name__)


class TTSClient:
    """Client for Kokoro-FastAPI text-to-speech service."""

    def __init__(self, mock: bool = False):
        self.mock = mock
        self.base_url = config.KOKORO_BASE_URL
        self.voice = config.KOKORO_VOICE
        self.speed = config.KOKORO_SPEED
        self.response_format = config.KOKORO_RESPONSE_FORMAT

        if not mock:
            logger.info(f"TTS Client connecting to {self.base_url}")
        else:
            logger.info("TTS Client running in MOCK MODE")

    def health_check(self) -> bool:
        """Check if the Kokoro-FastAPI server is running."""
        if self.mock:
            return True
        try:
            # Kokoro-FastAPI exposes a models endpoint
            resp = requests.get(
                f"{self.base_url}/models",
                timeout=5
            )
            return resp.status_code == 200
        except Exception:
            return False

    def list_voices(self) -> list[str]:
        """List available voices from the Kokoro server."""
        if self.mock:
            return ["af_heart", "af_bella", "am_adam", "am_michael",
                    "bf_emma", "bm_george"]
        try:
            resp = requests.get(
                f"{self.base_url}/audio/voices",
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            # Try the models endpoint as fallback
            resp = requests.get(f"{self.base_url}/models", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("id", "") for m in data.get("data", [])]
            return []
        except Exception as e:
            logger.warning(f"Failed to list voices: {e}")
            return []

    def synthesize(self, text: str, output_path: str,
                   voice: str = None, speed: float = None) -> bool:
        """
        Convert text to speech and save as an audio file.

        Args:
            text: The text to synthesize.
            output_path: Where to save the audio file.
            voice: Override the default voice.
            speed: Override the default speed.

        Returns:
            True if synthesis succeeded, False otherwise.
        """
        if self.mock:
            return self._mock_synthesize(text, output_path)

        voice = voice or self.voice
        speed = speed or self.speed

        # Kokoro-FastAPI accepts the OpenAI TTS API format
        payload = {
            "model": "kokoro",
            "input": text,
            "voice": voice,
            "speed": speed,
            "response_format": self.response_format,
        }

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                logger.debug(f"  TTS request: {len(text)} chars, voice={voice}")
                start_time = time.time()

                resp = requests.post(
                    f"{self.base_url}/audio/speech",
                    json=payload,
                    timeout=300,  # long timeout for large texts
                    stream=True,
                )
                resp.raise_for_status()

                # Stream the response to disk
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                bytes_written = 0
                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                        bytes_written += len(chunk)

                elapsed = time.time() - start_time
                size_mb = bytes_written / (1024 * 1024)
                logger.info(f"  TTS complete: {size_mb:.1f} MB in {elapsed:.1f}s "
                            f"→ {output_path}")
                return True

            except requests.exceptions.Timeout:
                logger.warning(f"  TTS attempt {attempt}: timeout")
            except requests.exceptions.ConnectionError:
                logger.warning(f"  TTS attempt {attempt}: connection error "
                               "— is Kokoro-FastAPI running?")
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response else "?"
                logger.warning(f"  TTS attempt {attempt}: HTTP {status}")
                # Don't retry on 4xx
                if e.response and e.response.status_code < 500:
                    break
            except Exception as e:
                logger.warning(f"  TTS attempt {attempt}: {e}")

            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_DELAY)

        logger.error(f"  TTS failed after {config.MAX_RETRIES} attempts")
        return False

    def synthesize_long_text(self, text: str, output_path: str,
                             voice: str = None, speed: float = None,
                             chunk_size: int = 4000) -> bool:
        """
        Synthesize long text by splitting into chunks if needed.

        Kokoro-FastAPI handles long text internally, but for very long
        articles (10k+ words) it can be more reliable to split at
        paragraph boundaries and concatenate the audio.

        For now, this just passes through to synthesize() since
        Kokoro-FastAPI handles chunking. If we hit reliability issues
        with very long texts, we can implement client-side chunking here.
        """
        return self.synthesize(text, output_path, voice, speed)

    def _mock_synthesize(self, text: str, output_path: str) -> bool:
        """
        Create a tiny valid MP3 file for testing.

        This writes a minimal MP3 frame so that downstream code
        (metadata tagging, RSS generation) has a real file to work with.
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Minimal valid MP3 frame (silence, ~0.026s)
        # MPEG1 Layer3, 128kbps, 44100Hz, stereo
        mp3_frame = bytes([
            0xFF, 0xFB, 0x90, 0x00,  # MP3 header
        ] + [0x00] * 413)  # frame data (417 bytes total for this config)

        # Write a few frames so it's a recognizable MP3
        with open(output_path, "wb") as f:
            for _ in range(10):
                f.write(mp3_frame)

        word_count = len(text.split())
        logger.info(f"  [MOCK] TTS: {word_count} words → {output_path}")
        time.sleep(0.05)  # simulate tiny delay
        return True
