"""Floating overlay window for real-time transcription display.

Uses a subprocess to run tkinter in its own process, since macOS requires
tkinter to run on the main thread. Communication via a simple socket.
"""

import json
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OVERLAY_PORT = 19876  # Local socket for IPC


class TranscriptionOverlay:
    """Controls a floating overlay window in a separate process.

    Sends commands via localhost UDP socket to avoid threading issues
    with tkinter on macOS.
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None

    def start(self) -> bool:
        """Start the overlay subprocess."""
        try:
            # Launch overlay process
            overlay_script = Path(__file__).parent / "_overlay_process.py"
            self._process = subprocess.Popen(
                [sys.executable, str(overlay_script)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Create UDP socket for sending commands
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            # Wait for process to be ready
            time.sleep(0.5)
            if self._process.poll() is not None:
                logger.warning("Overlay process exited immediately")
                return False

            logger.info("Overlay process started")
            return True
        except Exception as e:
            logger.warning(f"Failed to start overlay: {e}")
            return False

    def stop(self):
        """Stop the overlay subprocess."""
        self._send_cmd("quit")
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        if self._sock:
            self._sock.close()
            self._sock = None

    def show(self, text: str, status: str = ""):
        """Show or update the overlay with text. Thread-safe."""
        self._send_cmd("show", text=text, status=status)

    def hide(self):
        """Hide the overlay. Thread-safe."""
        self._send_cmd("hide")

    def _send_cmd(self, cmd: str, **kwargs):
        """Send a command to the overlay process via UDP."""
        if not self._sock:
            return
        try:
            msg = json.dumps({"cmd": cmd, **kwargs}).encode()
            self._sock.sendto(msg, ("127.0.0.1", OVERLAY_PORT))
        except Exception:
            pass
