"""Qwen3-ASR streaming worker process.

Runs as a separate process using ~/.hotmic/venv/bin/python.
Loads the Qwen3-ASR model once and serves streaming transcription
requests over a TCP socket using length-prefixed JSON messages.

Protocol:
  Each message is: [4-byte uint32 BE length][JSON payload]
  Audio PCM is base64-encoded float32 in the JSON.

Commands:
  init_stream  — start a new streaming session
  feed_audio   — feed incremental PCM audio, returns current text
  finish       — finalize streaming, returns final text
  health       — returns status and model info
  shutdown     — clean exit
"""

import base64
import json
import logging
import signal
import socket
import struct
import sys
import threading
import traceback
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global state
_session = None
_streaming_state = None
_model_name = None
_lock = threading.Lock()


def load_model(model_name: str):
    """Load the Qwen3-ASR model via Session API."""
    global _session, _model_name
    from mlx_qwen3_asr import Session
    logger.info(f"Loading model: {model_name}")
    _session = Session(model=model_name)
    _model_name = model_name
    logger.info("Model loaded successfully")


def handle_init_stream(params: dict) -> dict:
    """Initialize a new streaming session."""
    global _streaming_state
    _streaming_state = None  # clear any stale state first
    language = params.get("language", "zh")

    _streaming_state = _session.init_streaming(
        language=language,
        chunk_size_sec=2.0,
        max_context_sec=30.0,
        sample_rate=16000,
    )
    return {"status": "ok"}


def handle_feed_audio(params: dict) -> dict:
    """Feed incremental PCM audio and return current transcription."""
    global _streaming_state
    if _streaming_state is None:
        return {"error": "No active streaming session. Call init_stream first."}

    pcm_b64 = params.get("pcm_b64", "")
    if not pcm_b64:
        return {"error": "Missing pcm_b64"}

    pcm_bytes = base64.b64decode(pcm_b64)
    pcm = np.frombuffer(pcm_bytes, dtype=np.float32)

    _streaming_state = _session.feed_audio(pcm, _streaming_state)

    return {
        "text": _streaming_state.text,
        "stable_text": getattr(_streaming_state, "stable_text", _streaming_state.text),
    }


def handle_finish(params: dict) -> dict:
    """Finalize the streaming session."""
    global _streaming_state
    if _streaming_state is None:
        return {"error": "No active streaming session."}

    _streaming_state = _session.finish_streaming(_streaming_state)
    text = _streaming_state.text
    language = getattr(_streaming_state, "language", "")
    _streaming_state = None

    return {"text": text, "language": language}


def handle_health(params: dict) -> dict:
    """Return health status."""
    return {
        "status": "ok",
        "model": _model_name or "not loaded",
        "has_session": _streaming_state is not None,
    }


def handle_shutdown(params: dict) -> dict:
    """Signal shutdown."""
    return {"status": "ok", "shutdown": True}


HANDLERS = {
    "init_stream": handle_init_stream,
    "feed_audio": handle_feed_audio,
    "finish": handle_finish,
    "health": handle_health,
    "shutdown": handle_shutdown,
}


def recv_message(conn: socket.socket) -> dict:
    """Read a length-prefixed JSON message from socket."""
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("Connection closed")
        header += chunk

    length = struct.unpack(">I", header)[0]
    if length > 10 * 1024 * 1024:  # 10MB max
        raise ValueError(f"Message too large: {length}")

    data = b""
    while len(data) < length:
        chunk = conn.recv(min(length - len(data), 65536))
        if not chunk:
            raise ConnectionError("Connection closed during message")
        data += chunk

    return json.loads(data)


def send_message(conn: socket.socket, msg: dict):
    """Send a length-prefixed JSON message to socket."""
    data = json.dumps(msg).encode()
    conn.sendall(struct.pack(">I", len(data)) + data)


def handle_connection(conn: socket.socket, addr):
    """Handle a single client connection (blocking, one message at a time)."""
    try:
        while True:
            try:
                msg = recv_message(conn)
            except ConnectionError:
                break

            cmd = msg.get("cmd", "")
            handler = HANDLERS.get(cmd)
            if not handler:
                send_message(conn, {"error": f"Unknown command: {cmd}"})
                continue

            with _lock:
                try:
                    result = handler(msg)
                except Exception as e:
                    logger.error(f"Error handling {cmd}: {e}\n{traceback.format_exc()}")
                    result = {"error": str(e)}

            send_message(conn, result)

            if result.get("shutdown"):
                conn.close()
                return True  # signal to stop server
    except Exception as e:
        logger.error(f"Connection error from {addr}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return False


def run_server(port: int, model_name: str):
    """Run the TCP server."""
    load_model(model_name)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.settimeout(1.0)  # allow periodic shutdown check

    logger.info(f"ASR worker listening on 127.0.0.1:{port}")

    # Write ready signal to stdout for the manager to detect
    print(f"READY:{port}", flush=True)

    running = True

    def handle_signal(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running:
        try:
            conn, addr = server.accept()
            conn.settimeout(300)  # 5 min idle timeout
            should_stop = handle_connection(conn, addr)
            if should_stop:
                running = False
        except socket.timeout:
            continue
        except Exception as e:
            if running:
                logger.error(f"Accept error: {e}")

    server.close()
    logger.info("ASR worker stopped")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Qwen3-ASR streaming worker")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--model", type=str, default="Qwen/Qwen3-ASR-1.7B")
    args = parser.parse_args()

    run_server(args.port, args.model)


if __name__ == "__main__":
    main()
