# HotMic

Push-to-talk voice dictation for macOS. Hold a hotkey, speak, and your words auto-paste into any app — powered by [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) streaming on Apple Silicon.

> Built on [enesbasbug/hotmic](https://github.com/enesbasbug/hotmic), enhanced with unified Qwen3-ASR streaming, real-time overlay, and auto-paste.

## Features

- **Works with any app** — not tied to any specific tool; dictate into editors, browsers, terminals, chat apps
- **Real-time streaming transcription** — see your words appear as you speak via floating overlay
- **Single-model consistency** — both streaming preview and final output use the same Qwen3-ASR model
- **Local & private** — all processing on-device, no audio leaves your machine
- **Auto-paste** — transcribed text automatically pastes into your active window
- **Push-to-talk** — hold Ctrl+Alt to record, release to transcribe
- **Sub-second latency** — model resident in memory, 0.5s feed interval

## Quick Start

### Prerequisites

```bash
brew install cmake
xcode-select --install
```

### Install

```bash
git clone https://github.com/xiaohongsimon/hotmic.git
cd hotmic

# Install Qwen3-ASR (primary ASR engine)
python3 -m venv ~/.hotmic/venv
~/.hotmic/venv/bin/pip install mlx-qwen3-asr
# Model (~3GB) auto-downloads on first use

# Build whisper.cpp fallback + install dependencies
python3 scripts/setup.py
```

### Grant Permissions (macOS will prompt)

- **System Settings → Privacy & Security → Microphone** → allow Terminal/iTerm
- **System Settings → Privacy & Security → Accessibility** → allow Terminal/iTerm

### Run

```bash
python3 scripts/exec.py daemon start --background
```

**Hold Ctrl+Alt** → speak → release → text appears in your active window.

### Also Works as a Claude Code Plugin

```bash
/hotmic:setup
/hotmic:start
```

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Daemon (background process)                      │
│                                                   │
│  [Hotkey Listener] → [Audio Recorder]             │
│         ↓                    ↓                    │
│  [Overlay Display] ← [Streaming Transcriber]      │
│                              ↓ TCP (localhost)    │
│  [Auto-Paste] ← ─ ─ ─ ─ ─ ─┘                    │
└──────────────────────────┬────────────────────────┘
                           │
┌──────────────────────────┴────────────────────────┐
│  Qwen3-ASR Worker (separate process)              │
│  - Model loaded once, resident in MLX memory      │
│  - Native streaming API for incremental text      │
│  - 1.7B params, float16, Metal GPU accelerated    │
└───────────────────────────────────────────────────┘
```

### How It Works

1. **Hold hotkey** → saves frontmost app, starts mic, initializes streaming session
2. **Every 0.5s** → new audio chunk sent to worker via TCP, overlay updates incrementally
3. **Release hotkey** → `finish_streaming()` for final accurate transcription
4. **Auto-paste** → activates saved app via osascript, copies to clipboard, Cmd+V

### Text Injection Priority

1. **cmux** — native Claude Code integration (if available)
2. **osascript** — activate previous app + simulate Cmd+V (default)
3. **Clipboard** — last resort, manual Cmd+V

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `asr_backend` | `qwen3` | ASR engine: `qwen3` or `whisper` |
| `qwen3_model` | `Qwen/Qwen3-ASR-1.7B` | Qwen3-ASR model |
| `feed_interval` | `0.5` | Seconds between audio feeds |
| `language` | `zh` | Language code |
| `hotkey` | `ctrl+alt` | Hotkey combo |
| `overlay_enabled` | `true` | Floating transcription overlay |

Config file: `~/.config/hotmic/config.json`

## Project Structure

```
hotmic/
├── scripts/
│   ├── exec.py                  # CLI entry point
│   └── setup.py                 # Bootstrap setup
├── src/hotmic/
│   ├── daemon.py                # Background daemon, hotkey handling
│   ├── config.py                # Configuration management
│   ├── qwen3_asr_worker.py      # Qwen3-ASR streaming worker (TCP server)
│   ├── qwen3_worker_manager.py  # Worker lifecycle management
│   ├── streaming_transcriber.py # TCP client for worker IPC
│   ├── whisper_server_manager.py# Whisper fallback server
│   ├── recorder.py              # Microphone capture
│   ├── transcriber.py           # Whisper CLI fallback
│   ├── overlay.py               # Floating overlay controller
│   ├── _overlay_process.py      # Tkinter overlay subprocess
│   ├── keyboard.py              # Text injection
│   └── sounds.py                # Audio feedback
├── commands/                     # Claude Code plugin docs
├── hooks/                        # Plugin lifecycle hooks
└── pyproject.toml
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No audio input | System Settings → Privacy & Security → Microphone |
| Hotkey not working | System Settings → Privacy & Security → Accessibility |
| Auto-paste not working | Grant Accessibility permission to Terminal/iTerm |
| Build failed | `brew install cmake && xcode-select --install` |

Logs: `tail -50 ~/.config/hotmic/daemon.log`

## Privacy

All processing is local. No audio sent anywhere. No telemetry.

## Acknowledgements

- [hotmic](https://github.com/enesbasbug/hotmic) by [@enesbasbug](https://github.com/enesbasbug) — original project that inspired HotMic
- [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) by Alibaba — speech recognition model
- [mlx-qwen3-asr](https://github.com/nicholasgasior/mlx-qwen3-asr) — MLX port for Apple Silicon
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) — fallback transcription engine

## License

MIT
