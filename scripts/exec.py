#!/usr/bin/env python3
"""
Entry point for hotmic commands.
Routes subcommands to appropriate handlers.
"""

import os
import sys
import argparse

# Add src directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PLUGIN_ROOT, "src")
sys.path.insert(0, SRC_DIR)


def main():
    parser = argparse.ArgumentParser(description="hotmic CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Daemon commands
    daemon_parser = subparsers.add_parser("daemon", help="Daemon management")
    daemon_parser.add_argument("action", choices=["start", "stop", "status", "restart", "run", "health"],
                               help="Action to perform")
    daemon_parser.add_argument("--background", "-b", action="store_true",
                               help="Run in background")
    daemon_parser.add_argument("--quiet", "-q", action="store_true",
                               help="Suppress output")
    daemon_parser.add_argument("--verbose", "-v", action="store_true",
                               help="Verbose output")

    # Config commands
    config_parser = subparsers.add_parser("config", help="Configuration management")
    config_parser.add_argument("setting", nargs="?",
                               choices=["show", "model", "hotkey", "output", "sounds"],
                               default="show", help="Setting to configure")
    config_parser.add_argument("value", nargs="?", help="New value")

    # Setup command
    setup_parser = subparsers.add_parser("setup", help="Run setup")
    setup_parser.add_argument("--skip-build", action="store_true",
                              help="Skip whisper.cpp build")
    setup_parser.add_argument("--skip-model", action="store_true",
                              help="Skip model download")

    args = parser.parse_args()

    if args.command == "daemon":
        handle_daemon(args)
    elif args.command == "config":
        handle_config(args)
    elif args.command == "setup":
        handle_setup(args)
    else:
        parser.print_help()
        sys.exit(1)


def handle_daemon(args):
    """Handle daemon commands."""
    from hotmic.daemon import (
        start_daemon, stop_daemon, daemon_status
    )
    from hotmic.config import Config
    import time

    config = Config.load()

    if args.action in ("start", "restart"):
        # Save cmux context so daemon can use cmux send
        _save_cmux_context()

    if args.action == "start":
        if not config.setup_complete:
            if not args.quiet:
                print("Setup not complete. Run /hotmic:setup first.")
            sys.exit(1)

        # start_daemon already checks if daemon is running
        start_daemon(background=args.background, quiet=args.quiet)

    elif args.action == "stop":
        stop_daemon()

    elif args.action == "restart":
        stop_daemon()
        time.sleep(0.5)
        start_daemon(background=args.background, quiet=args.quiet)

    elif args.action == "run":
        # Run in foreground (used by background launcher)
        start_daemon(background=False, quiet=args.quiet)

    elif args.action == "status":
        status = daemon_status()
        if args.verbose:
            print("HotMic Status")
            print("=" * 40)
            print(f"Setup:    {'Complete' if status['setup_complete'] else 'Not complete'}")
            if status['running']:
                print(f"Daemon:   Running (PID: {status['pid']})")
            else:
                print("Daemon:   Stopped")
            print(f"Model:    {status['model']}")
            print(f"Hotkey:   {status['hotkey']}")
            print(f"Output:   {status['output_mode']}")
        else:
            if status['running']:
                print(f"Running (PID: {status['pid']})")
            else:
                print("Stopped")

    elif args.action == "health":
        _run_health_check(config)


def handle_config(args):
    """Handle config commands."""
    from hotmic.config import Config, WHISPER_MODELS
    from pathlib import Path
    import json

    config = Config.load()

    if args.setting == "show" or args.setting is None:
        print("Current Configuration")
        print("=" * 40)
        print(f"Model:    {config.model}")
        print(f"Hotkey:   {config.get_hotkey_description()}")
        print(f"Output:   {config.output_mode}")
        print(f"Sounds:   {'enabled' if config.sound_effects else 'disabled'}")
        return

    if args.value is None:
        # Show current value
        if args.setting == "model":
            print(f"Current model: {config.model}")
            print("\nAvailable models: tiny, base, medium, large-v3")
        elif args.setting == "hotkey":
            print(f"Current hotkey: {config.get_hotkey_description()}")
            print("\nOptions: ctrl, alt, shift, cmd (combine with +)")
        elif args.setting == "output":
            print(f"Current output mode: {config.output_mode}")
            print("\nOptions: keyboard, clipboard")
        elif args.setting == "sounds":
            print(f"Sound effects: {'on' if config.sound_effects else 'off'}")
        return

    # Set new value
    if args.setting == "model":
        if args.value not in WHISPER_MODELS:
            print(f"Invalid model: {args.value}")
            print("Available: tiny, base, medium, large-v3")
            sys.exit(1)

        # Check if model exists
        if config.models_dir:
            model_path = Path(config.models_dir) / WHISPER_MODELS[args.value]["file"]
            if not model_path.exists():
                print(f"Model '{args.value}' not downloaded.")
                print(f"Download with:")
                print(f"  cd ~/.local/share/hotmic/whisper.cpp")
                print(f"  ./models/download-ggml-model.sh {args.value}")
                sys.exit(1)

        config.model = args.value
        config.save()
        print(f"Model changed to: {args.value}")
        print("Restart daemon for changes to take effect.")

    elif args.setting == "hotkey":
        keys = args.value.lower().split("+")
        config.hotkey_ctrl = "ctrl" in keys
        config.hotkey_alt = "alt" in keys
        config.hotkey_shift = "shift" in keys
        config.hotkey_cmd = "cmd" in keys
        config.save()
        print(f"Hotkey changed to: {config.get_hotkey_description()}")
        print("Restart daemon for changes to take effect.")

    elif args.setting == "output":
        if args.value not in ["keyboard", "clipboard"]:
            print("Invalid output mode. Options: keyboard, clipboard")
            sys.exit(1)
        config.output_mode = args.value
        config.save()
        print(f"Output mode changed to: {args.value}")
        print("Restart daemon for changes to take effect.")

    elif args.setting == "sounds":
        config.sound_effects = args.value.lower() in ["on", "true", "1", "yes"]
        config.save()
        print(f"Sound effects: {'on' if config.sound_effects else 'off'}")


def _run_health_check(config):
    """Run a comprehensive health check and auto-fix issues."""
    from hotmic.daemon import is_daemon_running, read_pid_file, start_daemon, stop_daemon
    import socket
    import time

    issues = []
    fixed = []

    # 1. Check daemon process
    if not is_daemon_running():
        issues.append("Daemon not running")
        print("Daemon: NOT RUNNING — restarting...")
        start_daemon(background=True, quiet=True)
        time.sleep(3)
        if is_daemon_running():
            fixed.append("Daemon restarted")
            print("Daemon: FIXED — restarted successfully")
        else:
            print("Daemon: FAILED — could not restart")
    else:
        pid = read_pid_file()
        print(f"Daemon: OK (PID: {pid})")

    # 2. Check ASR worker (if qwen3 backend)
    if config.asr_backend == "qwen3":
        try:
            sock = socket.create_connection(("127.0.0.1", config.asr_worker_port), timeout=2)
            sock.close()
            print(f"ASR Worker: OK (port {config.asr_worker_port})")
        except (OSError, socket.timeout):
            issues.append("ASR worker not reachable")
            print(f"ASR Worker: NOT REACHABLE on port {config.asr_worker_port}")
            if is_daemon_running():
                print("  Restarting daemon to recover worker...")
                stop_daemon()
                time.sleep(1)
                start_daemon(background=True, quiet=True)
                time.sleep(5)
                try:
                    sock = socket.create_connection(("127.0.0.1", config.asr_worker_port), timeout=2)
                    sock.close()
                    fixed.append("ASR worker recovered via daemon restart")
                    print("  ASR Worker: FIXED — recovered after restart")
                except (OSError, socket.timeout):
                    print("  ASR Worker: STILL DOWN after restart")

    # 3. Check microphone
    try:
        import sounddevice as sd
        default_input = sd.query_devices(kind='input')
        dev_name = default_input.get('name', 'Unknown') if isinstance(default_input, dict) else 'Unknown'
        print(f"Microphone: OK ({dev_name})")
    except Exception as e:
        issues.append(f"Microphone error: {e}")
        print(f"Microphone: ERROR — {e}")

    # Summary
    if not issues:
        print("\nAll systems healthy.")
    elif len(issues) == len(fixed):
        print(f"\n{len(fixed)} issue(s) auto-fixed.")
    else:
        unfixed = len(issues) - len(fixed)
        print(f"\n{unfixed} issue(s) need manual attention.")


def _save_cmux_context():
    """Save cmux env vars so the background daemon can use cmux send."""
    import json
    ctx = {}
    for key in ("CMUX_SOCKET", "CMUX_SURFACE_ID", "CMUX_WORKSPACE_ID", "CMUX_SOCKET_PATH"):
        val = os.environ.get(key)
        if val:
            ctx[key] = val
    if ctx:
        ctx_file = os.path.join(os.path.expanduser("~"), ".config", "hotmic", "cmux_context.json")
        os.makedirs(os.path.dirname(ctx_file), exist_ok=True)
        with open(ctx_file, "w") as f:
            json.dump(ctx, f)


def handle_setup(args):
    """Handle setup command."""
    from scripts.setup import run_setup
    run_setup(skip_build=args.skip_build, skip_model=args.skip_model)


if __name__ == "__main__":
    main()
