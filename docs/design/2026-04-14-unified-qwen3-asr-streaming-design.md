# Unified Qwen3-ASR Streaming Transcription

**Date:** 2026-04-14
**Status:** Approved

## Problem

The current system uses two different models for transcription:
- **Streaming chunks** (real-time overlay): whisper-server (whisper.cpp base model)
- **Final transcription**: Qwen3-ASR CLI (`mlx-qwen3-asr`)

This creates inconsistency — the real-time text shown during recording differs from the final output, resulting in poor user experience.

## Solution

Replace both transcription paths with a single **Qwen3-ASR streaming worker process** that uses the native `mlx-qwen3-asr` Streaming API. Both real-time chunks and final transcription go through the same model via `init_streaming()` → `feed_audio()` → `finish_streaming()`.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  daemon.py                                        │
│                                                   │
│  [AudioRecorder] ──PCM chunks──▶ [StreamingTranscriber]
│                                       │           │
│  [Overlay] ◀── text updates ──────────┘           │
│  [TextInjector] ◀── final text                    │
└────────────────────────┬──────────────────────────┘
                         │ TCP localhost:8788
                         │ length-prefixed JSON
                         ▼
┌──────────────────────────────────────────────────┐
│  qwen3_asr_worker.py                              │
│  (runs in ~/.hotmic/venv/bin/python)           │
│                                                   │
│  mlx-qwen3-asr Session + Streaming API            │
│  Model resident in MLX memory                     │
│                                                   │
│  Commands: init_stream, feed_audio, finish,       │
│            health, shutdown                        │
└──────────────────────────────────────────────────┘
```

## IPC Protocol

TCP socket on `localhost:8788`. Each message:

```
┌──────────┬──────────────────────┐
│ 4 bytes  │ N bytes              │
│ uint32BE │ JSON payload         │
│ (length) │                      │
└──────────┴──────────────────────┘
```

Audio data is base64-encoded float32 PCM in JSON (2s@16kHz = 128KB → base64 ~170KB, negligible on localhost).

### Commands

**init_stream** — called when recording starts:
```json
→ {"cmd": "init_stream", "language": "zh"}
← {"status": "ok"}
```

**feed_audio** — called every ~0.5s with new PCM data (incremental, not cumulative):
```json
→ {"cmd": "feed_audio", "pcm_b64": "<base64 float32 array>"}
← {"text": "你好世界", "stable_text": "你好"}
```

**finish** — called when user releases hotkey:
```json
→ {"cmd": "finish"}
← {"text": "你好世界这是一段测试", "language": "zh"}
```

**health** — liveness check:
```json
→ {"cmd": "health"}
← {"status": "ok", "model": "Qwen/Qwen3-ASR-1.7B"}
```

**shutdown** — clean exit:
```json
→ {"cmd": "shutdown"}
← {"status": "ok"}
```

## Data Flow

### Current (cumulative re-transcription every 2s):
```
t=0s  start recording
t=3s  send [0-3s] full audio → whisper-server → "你好"
t=5s  send [0-5s] full audio → whisper-server → "你好世界"
t=7s  send [0-7s] full audio → whisper-server → "你好世界这是"
release → send [0-7s] to Qwen3-ASR CLI → "你好世界这是一段测试"  (DIFFERENT MODEL)
```

### New (incremental feed every 0.5s):
```
t=0s   start → init_stream
t=0.5s feed [0-0.5s]   → text: ""
t=1.0s feed [0.5-1.0s] → text: ""
t=1.5s feed [1.0-1.5s] → text: "你好"
t=2.0s feed [1.5-2.0s] → text: "你好世界"
t=2.5s feed [2.0-2.5s] → text: "你好世界这是"
release → finish → "你好世界这是一段测试"  (SAME MODEL)
```

## File Changes

| File | Operation | Description |
|------|-----------|-------------|
| `src/hotmic/qwen3_asr_worker.py` | **New** | Worker subprocess: loads model, runs TCP server, handles streaming commands |
| `src/hotmic/qwen3_worker_manager.py` | **New** | Manages worker lifecycle (start/stop/health), replaces whisper_server_manager |
| `src/hotmic/streaming_transcriber.py` | **Rewrite** | TCP client to worker; feeds incremental audio, reads text updates |
| `src/hotmic/daemon.py` | **Modify** | Use QwenWorkerManager; change chunk timer to 0.5s incremental feed |
| `src/hotmic/config.py` | **Modify** | Add qwen3_model, qwen3_venv_path, asr_worker_port, feed_interval |
| `src/hotmic/whisper_server_manager.py` | **Keep** | Fallback when Qwen3-ASR unavailable |

## Config Changes

```python
asr_backend: str = "qwen3"                     # "qwen3" or "whisper"
qwen3_model: str = "Qwen/Qwen3-ASR-1.7B"       # model name/path
qwen3_venv_path: str = "~/.hotmic/venv"      # venv with mlx-qwen3-asr
asr_worker_port: int = 8788                     # worker TCP port
feed_interval: float = 0.5                      # audio feed interval (seconds)
```

## Error Handling

**Worker startup failure:** QwenWorkerManager detects worker not ready within 30s → falls back to whisper-server. Logs reason.

**Worker crash during recording:** StreamingTranscriber detects TCP disconnect → saves accumulated audio to WAV → falls back to one-shot Qwen3-ASR CLI or whisper-cli transcription. Overlay shows "降级转录中...".

**Model not found (first use):** Worker auto-downloads from HuggingFace (built into mlx-qwen3-asr). First start is slow (~3GB download), subsequent starts are fast.

**venv not found:** Check `~/.hotmic/venv/bin/python` exists. If not, warn and fall back to whisper-server.

## Key Design Decisions

1. **Separate process for model** — mlx-qwen3-asr lives in a different venv (`~/.hotmic/venv`), keeping plugin dependencies clean.
2. **TCP over UDP** — reliable delivery needed for audio data and command responses.
3. **Base64 for audio in JSON** — simplicity over efficiency; 170KB over localhost is negligible.
4. **0.5s feed interval** — faster than current 2s; streaming API handles internal buffering via `chunk_size_sec`.
5. **Keep whisper-server as fallback** — graceful degradation when Qwen3-ASR is unavailable.
6. **Incremental feed, not cumulative** — streaming API maintains state internally; we only send new audio.
