"""Standalone overlay process using tkinter.

Run as a separate process. Receives commands via UDP socket.
This avoids macOS tkinter main-thread requirement conflicts with the daemon.
"""

import json
import socket
import tkinter as tk
from typing import Optional

OVERLAY_PORT = 19876


class OverlayApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.88)
        # Prevent overlay from stealing focus on macOS
        try:
            self.root.tk.call(
                "::tk::unsupported::MacWindowStyle", "style",
                self.root._w, "utility", "noActivates"
            )
        except Exception:
            pass
        self.root.configure(bg="#1a1a2e")

        # Main frame
        frame = tk.Frame(self.root, bg="#1a1a2e", padx=16, pady=12)
        frame.pack(fill=tk.BOTH, expand=True)

        # Status label (small, top)
        self.status_label = tk.Label(
            frame,
            text="",
            font=("SF Pro Display", 11),
            fg="#ff4757",
            bg="#1a1a2e",
            anchor="w",
        )
        self.status_label.pack(fill=tk.X, pady=(0, 4))

        # Text label (main content)
        self.text_label = tk.Label(
            frame,
            text="",
            font=("SF Pro Display", 14),
            fg="#e8e8e8",
            bg="#1a1a2e",
            wraplength=620,
            justify="left",
            anchor="nw",
        )
        self.text_label.pack(fill=tk.BOTH, expand=True)

        # Track last shown content to avoid unnecessary geometry updates
        self._last_text = None
        self._last_status = None
        self._visible = False

        # Start hidden
        self.root.withdraw()

        # UDP listener
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", OVERLAY_PORT))
        self.sock.setblocking(False)

        # Poll for commands
        self.root.after(50, self._poll)

    def _poll(self):
        """Poll for UDP commands."""
        try:
            while True:
                data, _ = self.sock.recvfrom(4096)
                msg = json.loads(data.decode())
                cmd = msg.get("cmd")

                if cmd == "show":
                    self._do_show(msg.get("text", ""), msg.get("status", ""))
                elif cmd == "hide":
                    self._do_hide()
                elif cmd == "quit":
                    self.sock.close()
                    self.root.destroy()
                    return
        except BlockingIOError:
            pass
        except Exception:
            pass

        self.root.after(50, self._poll)

    def _do_show(self, text: str, status: str):
        """Update and show the overlay. Only resize when text length changes significantly."""
        text = text or ""
        status = status or ""

        # Update labels
        self.text_label.configure(text=text)
        self.status_label.configure(text=status)

        # Only recalculate geometry if text length changed significantly or first show
        text_changed = self._last_text is None or abs(len(text) - len(self._last_text or "")) > 20
        self._last_text = text
        self._last_status = status

        if text_changed or not self._visible:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            width = min(700, max(500, len(text) * 10 + 60)) if text else 500
            chars_per_line = 40
            num_lines = max(1, (len(text) // chars_per_line + 1)) if text else 1
            height = max(90, min(260, num_lines * 28 + 55))
            x = (screen_w - width) // 2
            y = screen_h - height - 130
            self.root.geometry(f"{width}x{height}+{x}+{y}")

        if not self._visible:
            self.root.deiconify()
            self.root.lift()
            self._visible = True

    def _do_hide(self):
        """Hide the overlay."""
        self.root.withdraw()
        self._visible = False
        self._last_text = None

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = OverlayApp()
    app.run()
