"""Whisper server lifecycle management."""

import os
import signal
import subprocess
import time
import logging
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from .config import Config, DEFAULT_CONFIG_DIR, WHISPER_MODELS

logger = logging.getLogger(__name__)

WHISPER_SERVER_PID_FILE = DEFAULT_CONFIG_DIR / "whisper-server.pid"
WHISPER_SERVER_LOG_FILE = DEFAULT_CONFIG_DIR / "whisper-server.log"


class WhisperServerManager:
    """Manages the whisper-server process lifecycle."""

    def __init__(self, config: Config):
        self.config = config
        self.port = config.whisper_server_port
        self.base_url = f"http://127.0.0.1:{self.port}"

    def _get_server_binary(self) -> Optional[Path]:
        """Find whisper-server binary."""
        if not self.config.whisper_cpp_path:
            return None
        cli_path = Path(self.config.whisper_cpp_path)
        # whisper-cli is at build/bin/whisper-cli, server is at build/bin/whisper-server
        server_path = cli_path.parent / "whisper-server"
        if server_path.exists():
            return server_path
        return None

    def start(self) -> bool:
        """Start whisper-server in background. Returns True if successful."""
        if self.is_running():
            logger.info("whisper-server already running")
            return True

        server_bin = self._get_server_binary()
        if not server_bin:
            logger.error("whisper-server binary not found")
            return False

        model_path = self.config.get_model_path()
        if not model_path or not model_path.exists():
            logger.error(f"Model not found: {model_path}")
            return False

        cmd = [
            str(server_bin),
            "--model", str(model_path),
            "--language", self.config.language,
            "--port", str(self.port),
            "--convert",
            "--no-timestamps",
            "--flash-attn",
            "--prompt", self._get_language_prompt(),
            "--no-fallback",
        ]

        try:
            DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(WHISPER_SERVER_LOG_FILE, "a") as log_file:
                process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                )

            # Write PID file
            WHISPER_SERVER_PID_FILE.write_text(str(process.pid))
            logger.info(f"whisper-server started (PID: {process.pid}, port: {self.port})")

            # Wait for server to be ready (up to 30s for large models)
            for i in range(60):
                time.sleep(0.5)
                # Check process is still alive
                if process.poll() is not None:
                    logger.error(f"whisper-server exited with code {process.returncode}")
                    self._cleanup_pid()
                    return False
                if self._health_check():
                    logger.info(f"whisper-server ready after {(i+1)*0.5:.1f}s")
                    return True

            logger.error("whisper-server failed to become ready in 30s")
            return False

        except Exception as e:
            logger.error(f"Failed to start whisper-server: {e}")
            return False

    def stop(self) -> None:
        """Stop whisper-server."""
        pid = self._read_pid()
        if pid is None:
            return

        try:
            os.kill(pid, signal.SIGTERM)
            # Wait for graceful shutdown
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                # Force kill if still running
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            logger.info("whisper-server stopped")
        except ProcessLookupError:
            logger.info("whisper-server was not running")
        finally:
            self._cleanup_pid()

    def is_running(self) -> bool:
        """Check if whisper-server is running (process + HTTP)."""
        pid = self._read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            self._cleanup_pid()
            return False
        return self._health_check()

    def ensure_running(self) -> bool:
        """Ensure whisper-server is running, start if needed."""
        if self.is_running():
            return True
        return self.start()

    def _get_language_prompt(self) -> str:
        """Get a transcription prompt appropriate for the configured language."""
        prompts = {
            "zh": "以下是普通话的语音转写。",
            "en": "The following is a voice transcription in English.",
            "ja": "以下は日本語の音声書き起こしです。",
            "ko": "다음은 한국어 음성 전사입니다.",
        }
        return prompts.get(self.config.language, "")

    def _health_check(self) -> bool:
        """Check if server responds to HTTP (any response = alive)."""
        import socket
        try:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            sock.close()
            return True
        except (OSError, socket.timeout):
            return False

    def _read_pid(self) -> Optional[int]:
        if WHISPER_SERVER_PID_FILE.exists():
            try:
                return int(WHISPER_SERVER_PID_FILE.read_text().strip())
            except (ValueError, FileNotFoundError):
                pass
        return None

    def _cleanup_pid(self) -> None:
        WHISPER_SERVER_PID_FILE.unlink(missing_ok=True)
