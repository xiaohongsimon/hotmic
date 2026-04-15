"""Streaming transcription via Qwen3-ASR worker (native streaming API)
or whisper-server HTTP API (fallback)."""

import base64
import json
import logging
import socket
import struct
import time
from typing import Optional

import numpy as np

from .config import Config, SAMPLE_RATE
from .transcriber import TranscriptionResult

logger = logging.getLogger(__name__)


class StreamingTranscriber:
    """Transcribes audio by communicating with the Qwen3-ASR worker process
    over TCP using the native streaming API.

    Falls back to whisper-server HTTP if worker is unavailable."""

    def __init__(self, config: Config):
        self.config = config
        self.port = config.asr_worker_port
        self._conn: Optional[socket.socket] = None
        self._connected = False
        # Track audio for fallback
        self._accumulated_audio: list[np.ndarray] = []

    def connect(self) -> bool:
        """Establish TCP connection to the worker."""
        if self._connected:
            return True
        try:
            self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._conn.settimeout(120)  # generous for first-time model download
            self._conn.connect(("127.0.0.1", self.port))
            self._connected = True
            return True
        except Exception as e:
            logger.debug(f"Worker connection failed: {e}")
            self._conn = None
            self._connected = False
            return False

    def disconnect(self):
        """Close TCP connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._connected = False

    def _send(self, msg: dict) -> dict:
        """Send a message and receive response."""
        if not self._conn:
            raise ConnectionError("Not connected")
        data = json.dumps(msg).encode()
        self._conn.sendall(struct.pack(">I", len(data)) + data)

        # Read response
        header = b""
        while len(header) < 4:
            chunk = self._conn.recv(4 - len(header))
            if not chunk:
                raise ConnectionError("Connection closed")
            header += chunk

        length = struct.unpack(">I", header)[0]
        resp_data = b""
        while len(resp_data) < length:
            chunk = self._conn.recv(min(length - len(resp_data), 65536))
            if not chunk:
                raise ConnectionError("Connection closed during response")
            resp_data += chunk

        return json.loads(resp_data)

    def init_stream(self) -> bool:
        """Initialize a new streaming session on the worker.
        Call this when recording starts."""
        self._accumulated_audio = []
        # Close any stale connection from a previous session
        if self._connected:
            self.disconnect()
        if not self.connect():
            return False
        try:
            resp = self._send({"cmd": "init_stream", "language": self.config.language})
            return resp.get("status") == "ok"
        except Exception as e:
            logger.error(f"init_stream failed: {e}")
            self.disconnect()
            return False

    def feed_audio(self, audio_chunk: np.ndarray) -> str:
        """Feed an incremental audio chunk and return current transcription.
        Returns empty string on error."""
        self._accumulated_audio.append(audio_chunk.copy())

        if not self._connected:
            return ""

        if len(audio_chunk) < 100:  # skip tiny fragments
            return ""

        try:
            pcm_b64 = base64.b64encode(audio_chunk.astype(np.float32).tobytes()).decode()
            resp = self._send({"cmd": "feed_audio", "pcm_b64": pcm_b64})
            if "error" in resp:
                logger.debug(f"feed_audio error: {resp['error']}")
                return ""
            return resp.get("text", "")
        except Exception as e:
            logger.debug(f"feed_audio failed: {e}")
            self.disconnect()
            return ""

    def finish_stream(self) -> TranscriptionResult:
        """Finalize the streaming session and return the complete transcription."""
        start = time.time()

        if self._connected:
            try:
                resp = self._send({"cmd": "finish"})
                elapsed = time.time() - start
                self.disconnect()

                if "error" in resp:
                    return self._fallback_transcribe(f"Worker error: {resp['error']}")

                text = resp.get("text", "").strip()
                if not text:
                    return TranscriptionResult(
                        text="", duration_seconds=elapsed,
                        model=self.config.qwen3_model,
                        success=False, error="No speech detected",
                    )

                return TranscriptionResult(
                    text=text, duration_seconds=elapsed,
                    model=self.config.qwen3_model, success=True,
                )
            except Exception as e:
                logger.error(f"finish_stream failed: {e}")
                self.disconnect()
                return self._fallback_transcribe(str(e))
        else:
            return self._fallback_transcribe("Worker not connected")

    def _fallback_transcribe(self, reason: str) -> TranscriptionResult:
        """Fallback: use accumulated audio with whisper-server or CLI."""
        logger.warning(f"Falling back to whisper transcription: {reason}")
        start = time.time()

        if not self._accumulated_audio:
            return TranscriptionResult(
                text="", duration_seconds=0,
                model="fallback", success=False,
                error="No audio accumulated",
            )

        audio = np.concatenate(self._accumulated_audio)
        if len(audio) < SAMPLE_RATE * 0.3:
            return TranscriptionResult(
                text="", duration_seconds=0,
                model="fallback", success=False,
                error="Recording too short",
            )

        # Try whisper-server
        try:
            wav_path = self._save_wav(audio)
            text = self._whisper_server_fallback(wav_path)
            wav_path.unlink(missing_ok=True)
            elapsed = time.time() - start
            if text:
                return TranscriptionResult(
                    text=text, duration_seconds=elapsed,
                    model="whisper-server-fallback", success=True,
                )
        except Exception as e:
            logger.debug(f"Whisper server fallback failed: {e}")

        return TranscriptionResult(
            text="", duration_seconds=time.time() - start,
            model="fallback", success=False,
            error=f"All transcription methods failed. Original: {reason}",
        )

    def _save_wav(self, audio: np.ndarray):
        """Save audio to temp WAV file."""
        import os
        import tempfile
        from pathlib import Path
        from scipy.io import wavfile

        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        wav_path = Path(path)
        audio_int16 = (audio * 32767).astype(np.int16)
        wavfile.write(str(wav_path), SAMPLE_RATE, audio_int16)
        return wav_path

    def _whisper_server_fallback(self, wav_path) -> str:
        """Transcribe via whisper-server HTTP API."""
        import subprocess
        url = f"http://127.0.0.1:{self.config.whisper_server_port}/inference"
        result = subprocess.run(
            ["curl", "-s", url,
             "-F", f"file=@{wav_path}",
             "-F", "temperature=0.0",
             "-F", "response_format=json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed: {result.stderr}")
        data = json.loads(result.stdout)
        return " ".join(data.get("text", "").strip().split())

    # Legacy API compatibility for non-streaming mode
    def transcribe_chunk(self, audio: np.ndarray) -> str:
        """Legacy: transcribe a full audio snapshot (for whisper backend)."""
        return self.feed_audio(audio)

    def transcribe_final(self, audio: np.ndarray) -> TranscriptionResult:
        """Legacy: transcribe complete audio."""
        return self.finish_stream()
