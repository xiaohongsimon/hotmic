"""Qwen3-ASR worker process lifecycle management."""

import os
import signal
import subprocess
import time
import logging
from pathlib import Path
from typing import Optional

from .config import Config, DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)

WORKER_PID_FILE = DEFAULT_CONFIG_DIR / "qwen3-worker.pid"
WORKER_LOG_FILE = DEFAULT_CONFIG_DIR / "qwen3-worker.log"


class Qwen3WorkerManager:
    """Manages the Qwen3-ASR worker process lifecycle."""

    def __init__(self, config: Config):
        self.config = config
        self.port = config.asr_worker_port
        self.model = config.qwen3_model
        self.venv_path = Path(config.qwen3_venv_path).expanduser()

    def _get_python(self) -> Optional[Path]:
        """Get the Python executable from the Qwen3 venv."""
        python = self.venv_path / "bin" / "python"
        if python.exists():
            return python
        python3 = self.venv_path / "bin" / "python3"
        if python3.exists():
            return python3
        return None

    def _get_worker_script(self) -> Path:
        """Get the worker script path."""
        return Path(__file__).parent / "qwen3_asr_worker.py"

    def start(self) -> bool:
        """Start the worker process. Returns True if successful."""
        if self.is_running():
            logger.info("Qwen3-ASR worker already running")
            return True

        python = self._get_python()
        if not python:
            logger.error(f"Python not found in venv: {self.venv_path}")
            return False

        worker_script = self._get_worker_script()
        if not worker_script.exists():
            logger.error(f"Worker script not found: {worker_script}")
            return False

        cmd = [
            str(python),
            str(worker_script),
            "--port", str(self.port),
            "--model", self.model,
        ]

        log_file = None
        try:
            DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            log_file = open(WORKER_LOG_FILE, "a")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=log_file,
                start_new_session=True,
            )

            # Parent process can close the log fd; child inherited it
            log_file.close()
            log_file = None

            # Write PID file
            WORKER_PID_FILE.write_text(str(process.pid))
            logger.info(f"Qwen3-ASR worker started (PID: {process.pid}, port: {self.port})")

            # Wait for READY signal. First-time model download can take minutes,
            # but local model load is ~2s. Use generous timeout for both cases.
            ready = False
            start_time = time.time()
            timeout = 600  # 10 min for first-time download

            while time.time() - start_time < timeout:
                if process.poll() is not None:
                    logger.error(f"Worker exited with code {process.returncode}")
                    self._cleanup_pid()
                    return False

                if process.stdout:
                    import select
                    readable, _, _ = select.select([process.stdout], [], [], 0.5)
                    if readable:
                        line = process.stdout.readline().decode().strip()
                        if line.startswith("READY:"):
                            ready = True
                            logger.info(f"Worker ready after {time.time() - start_time:.1f}s")
                            break
                else:
                    time.sleep(0.5)

            if process.stdout:
                process.stdout.close()

            if not ready:
                logger.error(f"Worker failed to become ready in {timeout}s")
                self.stop()
                return False

            return True

        except Exception as e:
            logger.error(f"Failed to start worker: {e}")
            return False
        finally:
            if log_file and not log_file.closed:
                log_file.close()

    def stop(self) -> None:
        """Stop the worker process."""
        pid = self._read_pid()
        if pid is None:
            return

        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            logger.info("Qwen3-ASR worker stopped")
        except ProcessLookupError:
            logger.info("Worker was not running")
        finally:
            self._cleanup_pid()

    def is_running(self) -> bool:
        """Check if worker is running (process alive + port reachable)."""
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
        """Ensure worker is running, start if needed."""
        if self.is_running():
            return True
        return self.start()

    def _health_check(self) -> bool:
        """Check if worker's TCP port is reachable."""
        import socket as _socket
        try:
            sock = _socket.create_connection(("127.0.0.1", self.port), timeout=2)
            sock.close()
            return True
        except (OSError, _socket.timeout):
            return False

    def _read_pid(self) -> Optional[int]:
        if WORKER_PID_FILE.exists():
            try:
                return int(WORKER_PID_FILE.read_text().strip())
            except (ValueError, FileNotFoundError):
                pass
        return None

    def _cleanup_pid(self) -> None:
        WORKER_PID_FILE.unlink(missing_ok=True)
