"""Background daemon for voice dictation hotkey listening."""

import os
import sys
import signal
import threading
import time
import logging
import subprocess
from pathlib import Path
from typing import Set, Optional

from pynput import keyboard

from .config import Config, DEFAULT_PID_FILE, DEFAULT_LOG_FILE, ensure_config_dir, get_plugin_root
from .recorder import AudioRecorder, MicrophoneError
from .transcriber import Transcriber
from .keyboard import TextInjector
from . import sounds

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VoiceDaemon:
    """Background daemon that listens for hotkeys and handles voice transcription."""

    def __init__(self, config: Config, quiet: bool = False):
        self.config = config
        self.quiet = quiet

        # Components
        self.recorder = AudioRecorder(max_seconds=config.max_recording_seconds)
        self.injector = TextInjector(mode=config.output_mode)

        # Streaming mode components
        self.server_manager = None
        self.streaming_transcriber = None
        self.overlay = None
        self._chunk_timer: Optional[threading.Timer] = None
        self._chunk_lock = threading.Lock()
        self._last_feed_index = 0
        self._frontmost_app: Optional[str] = None

        if config.streaming_mode:
            self._init_streaming(config)
        else:
            self.transcriber = Transcriber(config)

        # State
        self.is_recording = False
        self.pressed_keys: Set[keyboard.Key] = set()
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.running = False

        # Build required keys set based on config
        self.required_keys = self._build_required_keys()

    def _init_streaming(self, config: Config) -> None:
        """Initialize streaming mode components."""
        from .streaming_transcriber import StreamingTranscriber
        from .overlay import TranscriptionOverlay

        if config.asr_backend == "qwen3":
            from .qwen3_worker_manager import Qwen3WorkerManager
            self.server_manager = Qwen3WorkerManager(config)
        else:
            from .whisper_server_manager import WhisperServerManager
            self.server_manager = WhisperServerManager(config)

        self.streaming_transcriber = StreamingTranscriber(config)

        if config.overlay_enabled:
            self.overlay = TranscriptionOverlay()
            self.overlay.start()

        # Fallback transcriber for when server is down
        self.transcriber = Transcriber(config)

    def _build_required_keys(self) -> Set[keyboard.Key]:
        """Build set of required modifier keys from config.

        Maps each config flag to a canonical key (left side).
        _on_press/_on_release normalize right-side keys to left.
        """
        keys = set()
        if self.config.hotkey_ctrl:
            keys.add(keyboard.Key.ctrl_l)
        if self.config.hotkey_alt:
            keys.add(keyboard.Key.alt_l)
        if self.config.hotkey_shift:
            keys.add(keyboard.Key.shift_l)
        if self.config.hotkey_cmd:
            keys.add(keyboard.Key.cmd_l)
        return keys

    @staticmethod
    def _normalize_key(key: keyboard.Key) -> keyboard.Key:
        """Normalize right-side modifier keys to left-side equivalents."""
        mapping = {
            keyboard.Key.ctrl_r: keyboard.Key.ctrl_l,
            keyboard.Key.alt_r: keyboard.Key.alt_l,
            keyboard.Key.shift_r: keyboard.Key.shift_l,
            keyboard.Key.cmd_r: keyboard.Key.cmd_l,
        }
        return mapping.get(key, key)

    def _log(self, message: str) -> None:
        """Log message unless in quiet mode."""
        if not self.quiet:
            logger.info(message)

    def _on_press(self, key: keyboard.Key) -> None:
        """Handle key press."""
        key = self._normalize_key(key)
        if key in self.required_keys:
            self.pressed_keys.add(key)

            # Start recording when all required keys are pressed
            if self.pressed_keys == self.required_keys and not self.is_recording:
                self._start_recording()

    def _on_release(self, key: keyboard.Key) -> None:
        """Handle key release."""
        key = self._normalize_key(key)
        if key in self.required_keys:
            self.pressed_keys.discard(key)

            # Stop recording when any required key is released
            if self.is_recording:
                self._stop_recording()

    def _save_frontmost_app(self) -> None:
        """Save the currently focused app so we can restore it after transcription."""
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            self._frontmost_app = result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            self._frontmost_app = None

    def _activate_and_paste(self, text: str) -> bool:
        """Copy text to clipboard, activate the saved app, and paste."""
        try:
            # Copy to clipboard
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, env={"LANG": "en_US.UTF-8"})
            proc.communicate(text.encode("utf-8"))
            if proc.returncode != 0:
                return False

            # Activate the app that was frontmost when recording started
            if self._frontmost_app:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{self._frontmost_app}" to activate'],
                    timeout=2, capture_output=True,
                )
                import time as _time
                _time.sleep(0.15)

            # Paste via osascript (more reliable than pynput from background daemon)
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "v" using command down'],
                timeout=2, capture_output=True,
            )
            return True
        except Exception as e:
            self._log(f"activate_and_paste failed: {e}")
            return False

    def _start_recording(self) -> None:
        """Start recording audio."""
        self.is_recording = True
        self._log("Recording started...")
        self._last_feed_index = 0  # track how much audio we've sent
        self._save_frontmost_app()

        if self.config.sound_effects:
            threading.Thread(target=sounds.play_start_sound, daemon=True).start()

        # Show overlay
        if self.overlay:
            self.overlay.show("", status="Recording...")

        try:
            self.recorder.start()
        except MicrophoneError:
            # Audio device may have changed (e.g. Bluetooth headset reconnect).
            # Re-create recorder with fresh device list and retry once.
            self._log("Mic failed, re-initializing audio device...")
            try:
                self.recorder = AudioRecorder(max_seconds=self.config.max_recording_seconds)
                self.recorder.start()
                self._log("Audio device re-initialized successfully")
            except MicrophoneError as e:
                self._log(f"Microphone error after retry: {e}")
                if self.config.sound_effects:
                    threading.Thread(target=sounds.play_error_sound, daemon=True).start()
                if self.overlay:
                    self.overlay.show("Mic error - check audio device", status="Error")
                    threading.Timer(3.0, self.overlay.hide).start()
                self.is_recording = False
                return

        try:
            # Initialize streaming session and start feed timer
            if self.config.streaming_mode and self.streaming_transcriber:
                if self.config.asr_backend == "qwen3":
                    self.streaming_transcriber.init_stream()
                self._start_chunk_timer()
        except Exception as e:
            self._log(f"Streaming init error: {e}")
            self.is_recording = False

    def _start_chunk_timer(self) -> None:
        """Start periodic chunk transcription / audio feed."""
        self._chunk_count = 0
        interval = self.config.feed_interval if self.config.asr_backend == "qwen3" else 2.0
        initial_delay = 1.0 if self.config.asr_backend == "qwen3" else 3.0

        def do_chunk():
            if not self.is_recording:
                return
            self._chunk_count += 1
            self._transcribe_current_chunk()
            # Schedule next chunk
            with self._chunk_lock:
                if self.is_recording:
                    self._chunk_timer = threading.Timer(interval, do_chunk)
                    self._chunk_timer.daemon = True
                    self._chunk_timer.start()

        with self._chunk_lock:
            self._chunk_timer = threading.Timer(initial_delay, do_chunk)
            self._chunk_timer.daemon = True
            self._chunk_timer.start()

    def _stop_chunk_timer(self) -> None:
        """Stop the chunk timer."""
        with self._chunk_lock:
            if self._chunk_timer:
                self._chunk_timer.cancel()
                self._chunk_timer = None

    def _transcribe_current_chunk(self) -> None:
        """Feed new audio to the streaming transcriber and update overlay."""
        if not self.streaming_transcriber or not self.recorder.audio_data:
            return

        try:
            import numpy as np

            if self.config.asr_backend == "qwen3":
                # Incremental feed: only send new audio since last feed
                current_chunks = list(self.recorder.audio_data)
                if self._last_feed_index >= len(current_chunks):
                    return
                new_chunks = current_chunks[self._last_feed_index:]
                self._last_feed_index = len(current_chunks)
                new_audio = np.concatenate(new_chunks)
                text = self.streaming_transcriber.feed_audio(new_audio)
            else:
                # Legacy whisper mode: send full audio snapshot
                audio_snapshot = np.concatenate(list(self.recorder.audio_data))
                if len(audio_snapshot) < 16000 * 2:
                    return
                text = self.streaming_transcriber.transcribe_chunk(audio_snapshot)

            if text and self.overlay:
                self.overlay.show(text, status="Recording...")
                self._log(f"Chunk: {text[:50]}...")
        except Exception as e:
            self._log(f"Chunk transcription error: {e}")

    def _stop_recording(self) -> None:
        """Stop recording and process audio."""
        if not self.is_recording:
            return

        self.is_recording = False
        self._stop_chunk_timer()
        self._log("Recording stopped, processing...")

        if self.config.sound_effects:
            threading.Thread(target=sounds.play_stop_sound, daemon=True).start()

        # Update overlay
        if self.overlay:
            self.overlay.show("", status="Transcribing...")

        # Stop recording and get audio
        audio = self.recorder.stop()

        # Process in background thread
        threading.Thread(
            target=self._process_audio,
            args=(audio,),
            daemon=True
        ).start()

    def _process_audio(self, audio) -> None:
        """Process recorded audio (runs in background thread)."""
        if audio is None:
            self._log("No audio recorded")
            if self.streaming_transcriber and self.config.asr_backend == "qwen3":
                self.streaming_transcriber.disconnect()
            if self.overlay:
                self.overlay.hide()
            return

        duration = self.recorder.get_duration(audio)
        if duration < 0.3:
            self._log("Recording too short, ignoring")
            if self.streaming_transcriber and self.config.asr_backend == "qwen3":
                self.streaming_transcriber.disconnect()
            if self.overlay:
                self.overlay.show("Recording too short", status="")
                threading.Timer(1.5, self.overlay.hide).start()
            return

        self._log(f"Audio duration: {duration:.1f}s")

        try:
            # Use streaming transcriber if available, else fallback
            if self.config.streaming_mode and self.streaming_transcriber:
                if self.config.asr_backend == "qwen3":
                    result = self.streaming_transcriber.finish_stream()
                else:
                    result = self.streaming_transcriber.transcribe_final(audio)
            else:
                wav_path = self.recorder.save_to_wav(audio)
                self._log(f"Saved to: {wav_path}")
                result = self.transcriber.transcribe(wav_path)
                wav_path.unlink(missing_ok=True)

            if result.success:
                self._log(f"Transcribed ({result.duration_seconds:.1f}s): {result.text[:50]}...")

                # Show final text in overlay
                if self.overlay:
                    self.overlay.show(result.text, status="Done")

                # Inject text via cmux send (works with Claude Code in cmux)
                try:
                    cmux_bin = "/Applications/cmux.app/Contents/Resources/bin/cmux"
                    cmux_sock = str(Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock")
                    cmux_env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
                                "CMUX_SOCKET_PATH": cmux_sock}
                    # Read saved surface ref
                    cmux_ctx_file = Path.home() / ".config" / "hotmic" / "cmux_context.json"
                    surface = "surface:2"
                    if cmux_ctx_file.exists():
                        import json as _json
                        ctx = _json.loads(cmux_ctx_file.read_text())
                        cmux_env.update(ctx)
                    # Step 1: set-buffer
                    r1 = subprocess.run(
                        [cmux_bin, "set-buffer", "--name", "voice", result.text],
                        timeout=3, capture_output=True, text=True, env=cmux_env,
                    )
                    # Step 2: paste-buffer
                    r2 = subprocess.run(
                        [cmux_bin, "paste-buffer", "--name", "voice", "--surface", surface],
                        timeout=3, capture_output=True, text=True, env=cmux_env,
                    )
                    if r1.returncode == 0 and r2.returncode == 0:
                        self._log("Text pasted via cmux buffer")
                    else:
                        self._log(f"cmux failed: set={r1.returncode} paste={r2.returncode} {r2.stderr.strip()}, trying osascript paste")
                        if self._activate_and_paste(result.text):
                            self._log("Text pasted via osascript")
                        else:
                            TextInjector.copy_to_clipboard(result.text)
                            self._log("osascript paste failed, text in clipboard")
                except Exception as e:
                    self._log(f"Send failed ({e}), trying osascript paste")
                    if not self._activate_and_paste(result.text):
                        TextInjector.copy_to_clipboard(result.text)
                        self._log("All paste methods failed, text in clipboard")
                if self.config.sound_effects:
                    threading.Thread(target=sounds.play_success_sound, daemon=True).start()

                # Hide overlay after delay
                if self.overlay:
                    threading.Timer(1.5, self.overlay.hide).start()
            else:
                self._log(f"Transcription failed: {result.error}")
                if self.overlay:
                    self.overlay.show(f"Error: {result.error}", status="")
                    threading.Timer(2.0, self.overlay.hide).start()
                if self.config.sound_effects:
                    threading.Thread(target=sounds.play_error_sound, daemon=True).start()

        except Exception as e:
            self._log(f"Error processing audio: {e}")
            if self.overlay:
                self.overlay.hide()
            if self.config.sound_effects:
                threading.Thread(target=sounds.play_error_sound, daemon=True).start()

    @staticmethod
    def _check_accessibility() -> bool:
        """Check if the process has macOS Accessibility permission (needed for hotkeys)."""
        try:
            import subprocess as _sp
            # This AppleScript check returns quickly and is reliable
            result = _sp.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def start(self) -> None:
        """Start the daemon."""
        if not self.config.setup_complete:
            print("Setup not complete. Run /hotmic:setup first.")
            sys.exit(1)

        # Check accessibility permission before starting
        if not self._check_accessibility():
            print("\n" + "=" * 50)
            print("ERROR: Accessibility permission not granted!")
            print("=" * 50)
            print("\nHotMic needs Accessibility permission to detect hotkeys.")
            print("\nGrant it in:")
            print("  System Settings → Privacy & Security → Accessibility")
            print("  → Enable your terminal app (Terminal / iTerm / etc.)")
            print("\nThen restart the daemon.")
            print("=" * 50)
            sys.exit(1)

        self.running = True

        # Start ASR backend if streaming mode
        if self.config.streaming_mode and self.server_manager:
            backend_name = "Qwen3-ASR worker" if self.config.asr_backend == "qwen3" else "whisper-server"
            print(f"Starting {backend_name} (this may take a moment for large models)...")
            if not self.server_manager.ensure_running():
                if self.config.asr_backend == "qwen3":
                    print("Warning: Qwen3-ASR worker failed to start, trying whisper-server fallback...")
                    from .whisper_server_manager import WhisperServerManager
                    self.server_manager = WhisperServerManager(self.config)
                    if not self.server_manager.ensure_running():
                        print("Warning: whisper-server also failed, falling back to whisper-cli")
                        self.config.streaming_mode = False
                    else:
                        self.config.asr_backend = "whisper"
                else:
                    print("Warning: whisper-server failed to start, falling back to whisper-cli")
                    self.config.streaming_mode = False

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Start keyboard listener
        self.keyboard_listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release
        )
        self.keyboard_listener.start()

        if not self.quiet:
            if self.config.asr_backend == "qwen3":
                mode = f"streaming (Qwen3-ASR, feed={self.config.feed_interval}s)"
                model_display = self.config.qwen3_model
            elif self.config.streaming_mode:
                mode = "streaming (whisper-server)"
                model_display = self.config.model
            else:
                mode = "local (whisper-cli)"
                model_display = self.config.model
            print("=" * 50)
            print("HotMic Daemon")
            print("=" * 50)
            print(f"Hotkey: {self.config.get_hotkey_description()}")
            print(f"Model: {model_display}")
            print(f"Language: {self.config.language}")
            print(f"Mode: {mode}")
            print(f"Output: {self.config.output_mode}")
            print(f"Overlay: {'on' if self.config.overlay_enabled else 'off'}")
            print("=" * 50)
            print("\nReady! Hold hotkey and speak.\n")

        # Keep running
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass

        self.stop()

    def stop(self) -> None:
        """Stop the daemon."""
        self.running = False
        self._stop_chunk_timer()

        if self.keyboard_listener:
            self.keyboard_listener.stop()
            self.keyboard_listener = None

        if self.overlay:
            self.overlay.stop()

        # Stop whisper-server
        if self.server_manager:
            self.server_manager.stop()

        self._log("Daemon stopped")

    def _handle_signal(self, signum, frame) -> None:
        """Handle shutdown signals."""
        self._log(f"Received signal {signum}, shutting down...")
        self.running = False


def write_pid_file() -> None:
    """Write PID to file."""
    ensure_config_dir()
    DEFAULT_PID_FILE.write_text(str(os.getpid()))


def remove_pid_file() -> None:
    """Remove PID file."""
    DEFAULT_PID_FILE.unlink(missing_ok=True)


def read_pid_file() -> Optional[int]:
    """Read PID from file."""
    if DEFAULT_PID_FILE.exists():
        try:
            return int(DEFAULT_PID_FILE.read_text().strip())
        except (ValueError, FileNotFoundError):
            pass
    return None


def is_daemon_running() -> bool:
    """Check if daemon is running."""
    pid = read_pid_file()
    if pid is None:
        return False

    try:
        # Check if process exists
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        # Process doesn't exist, clean up stale PID file
        remove_pid_file()
        return False


def start_daemon(background: bool = False, quiet: bool = False) -> None:
    """Start the daemon."""
    if is_daemon_running():
        print("Daemon is already running.")
        return

    config = Config.load()

    if background:
        # On macOS, avoid os.fork due to CoreFoundation issues in child process.
        # On Linux, fork works fine but we use subprocess for consistency.
        ensure_config_dir()

        plugin_root = get_plugin_root()
        exec_path = plugin_root / "scripts" / "exec.py"
        cmd = [sys.executable, str(exec_path), "daemon", "run"]
        if quiet:
            cmd.append("--quiet")

        try:
            with open(DEFAULT_LOG_FILE, "a") as log_file:
                process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                )
                launcher_pid = process.pid

            # Wait briefly for daemon to start and write PID file
            for _ in range(10):  # Wait up to 1 second
                time.sleep(0.1)
                daemon_pid = read_pid_file()
                if daemon_pid is not None:
                    print(f"Daemon started in background (PID: {daemon_pid})")
                    return

            # If we get here, daemon didn't write PID file
            # Check if the subprocess exited early
            exit_code = process.poll()
            if exit_code is not None:
                print(f"Error: Daemon process exited with code {exit_code}. Check {DEFAULT_LOG_FILE}")
                sys.exit(1)
            else:
                print(f"Warning: Daemon may have failed to start. Check {DEFAULT_LOG_FILE}")

        except (subprocess.SubprocessError, OSError) as e:
            print(f"Failed to start daemon: {e}")
            sys.exit(1)
        return

    write_pid_file()

    try:
        daemon = VoiceDaemon(config, quiet=quiet)
        daemon.start()
    finally:
        remove_pid_file()


def stop_daemon() -> None:
    """Stop the daemon."""
    pid = read_pid_file()
    if pid is None:
        print("Daemon is not running.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print("Daemon stopped.")
        remove_pid_file()
    except ProcessLookupError:
        print("Daemon was not running (stale PID file removed).")
        remove_pid_file()


def daemon_status() -> dict:
    """Get daemon status."""
    running = is_daemon_running()
    pid = read_pid_file() if running else None
    config = Config.load()

    return {
        "running": running,
        "pid": pid,
        "setup_complete": config.setup_complete,
        "model": config.model,
        "hotkey": config.get_hotkey_description(),
        "output_mode": config.output_mode
    }


def main():
    """CLI entry point for daemon."""
    import argparse

    parser = argparse.ArgumentParser(description="Voice-to-Claude daemon")
    parser.add_argument("action", choices=["start", "stop", "status", "restart"],
                       help="Action to perform")
    parser.add_argument("--background", "-b", action="store_true",
                       help="Run in background")
    parser.add_argument("--quiet", "-q", action="store_true",
                       help="Suppress output")

    args = parser.parse_args()

    if args.action == "start":
        start_daemon(background=args.background, quiet=args.quiet)
    elif args.action == "stop":
        stop_daemon()
    elif args.action == "restart":
        stop_daemon()
        time.sleep(0.5)
        start_daemon(background=args.background, quiet=args.quiet)
    elif args.action == "status":
        status = daemon_status()
        if status["running"]:
            print(f"Daemon is running (PID: {status['pid']})")
            print(f"  Model: {status['model']}")
            print(f"  Hotkey: {status['hotkey']}")
            print(f"  Output: {status['output_mode']}")
        else:
            print("Daemon is not running.")
            if not status["setup_complete"]:
                print("  Setup not complete. Run /hotmic:setup first.")


if __name__ == "__main__":
    main()
