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
        """Start the overlay subprocess with retry."""
        import os as _os
        overlay_script = Path(__file__).parent / "_overlay_process.py"

        # Try up to 3 times
        for attempt in range(3):
            try:
                # Kill any orphan overlay + anything holding the port
                try:
                    subprocess.run(["pkill", "-9", "-f", "_overlay_process"],
                                   timeout=2, capture_output=True)
                    result = subprocess.run(
                        ["lsof", "-ti", f":{OVERLAY_PORT}"],
                        capture_output=True, text=True, timeout=2,
                    )
                    for pid_str in result.stdout.strip().split():
                        try:
                            _os.kill(int(pid_str), 9)
                        except (ValueError, OSError):
                            pass
                except Exception:
                    pass
                time.sleep(0.5)

                # Launch overlay process
                self._process = subprocess.Popen(
                    [sys.executable, str(overlay_script)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                # Wait and verify
                time.sleep(0.7)
                if self._process.poll() is None:
                    # Verify port is actually bound
                    try:
                        test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        test_sock.bind(("127.0.0.1", OVERLAY_PORT))
                        test_sock.close()
                        # Port was free — overlay didn't bind it. That's a failure.
                        logger.warning(f"Overlay attempt {attempt+1}: process alive but port not bound")
                        self._process.kill()
                    except OSError:
                        # Port bound — overlay is good
                        logger.info(f"Overlay process started (attempt {attempt+1})")
                        return True
                else:
                    logger.warning(f"Overlay attempt {attempt+1}: process exited immediately")

            except Exception as e:
                logger.warning(f"Overlay attempt {attempt+1} error: {e}")

            time.sleep(0.5)

        logger.error("Overlay failed to start after 3 attempts")
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
