# Changelog

## [1.0.1] - 2026-04-15

### Fixed
- Accessibility permission check on daemon startup with clear guidance
- Right-side modifier keys (Ctrl_R/Alt_R) now trigger recording
- "Recording too short" overlay feedback instead of silent discard
- Generous timeouts for first-time model download (600s worker, 120s socket)
- Setup verifies Qwen3-ASR environment and model availability
- Auto-recovery on audio device change (Bluetooth headset reconnect)

### Changed
- Package renamed from `voice_to_claude` to `hotmic`
- Config directory: `~/.config/hotmic/`
- ASR venv directory: `~/.hotmic/venv/`
- README default language: Chinese
- Added CI workflow and badges

## [1.0.0] - 2026-04-14

### Added
- Unified Qwen3-ASR streaming transcription (single model for preview + final)
- Real-time floating overlay with 0.5s feed interval
- Auto-paste via osascript (saves frontmost app, activates, Cmd+V)
- Health check command (`daemon health`) with auto-fix
- Hotword dictionary support (context parameter)
- Graceful fallback chain: Qwen3-ASR → whisper-server → whisper-cli
- Push-to-talk with configurable hotkey (Ctrl+Alt default)

### Based on
- [enesbasbug/voice-to-claude](https://github.com/enesbasbug/voice-to-claude) — original project
