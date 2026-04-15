#!/bin/bash
set -e

# Build HotMic.dmg
# Usage: ./packaging/build-dmg.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/dist"
APP_NAME="HotMic"
APP_DIR="$BUILD_DIR/$APP_NAME.app"
DMG_PATH="$BUILD_DIR/$APP_NAME.dmg"

echo "Building $APP_NAME.app..."

# Clean
rm -rf "$APP_DIR" "$DMG_PATH"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# ── Info.plist ─────────────────────────────────────
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>HotMic</string>
    <key>CFBundleDisplayName</key>
    <string>HotMic</string>
    <key>CFBundleIdentifier</key>
    <string>com.hotmic.app</string>
    <key>CFBundleVersion</key>
    <string>1.0.1</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.1</string>
    <key>CFBundleExecutable</key>
    <string>hotmic-launcher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>HotMic needs microphone access for voice dictation.</string>
</dict>
</plist>
PLIST

# ── Launcher script ───────────────────────────────
cat > "$APP_DIR/Contents/MacOS/hotmic-launcher" <<'LAUNCHER'
#!/bin/bash

HOTMIC_DIR="$HOME/.hotmic"
APP_DIR="$HOTMIC_DIR/app"
LOG_FILE="$HOME/.config/hotmic/daemon.log"

# Check if installed
if [[ ! -d "$APP_DIR" || ! -f "$APP_DIR/.venv/bin/python" ]]; then
    # First launch — run installer in Terminal
    osascript <<EOF
tell application "Terminal"
    activate
    do script "echo '=== HotMic 首次安装 ===' && curl -sSL https://raw.githubusercontent.com/xiaohongsimon/hotmic/main/install.sh | bash && echo '' && echo '安装完成！请重新打开 HotMic.app' && echo '按任意键关闭...' && read -n 1"
end tell
EOF
    exit 0
fi

# Already installed — start/restart daemon
cd "$APP_DIR"
PYTHON=".venv/bin/python"

# Check if daemon is already running
PID_FILE="$HOME/.config/hotmic/daemon.pid"
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        # Already running, show notification
        osascript -e 'display notification "守护进程已在运行 (PID: '"$OLD_PID"')" with title "HotMic" subtitle "按住 Ctrl+Alt 说话"'
        exit 0
    fi
fi

# Start daemon
mkdir -p "$(dirname "$LOG_FILE")"
$PYTHON scripts/exec.py daemon start --background --quiet 2>>"$LOG_FILE"

# Wait for PID
sleep 2
if [[ -f "$PID_FILE" ]]; then
    NEW_PID=$(cat "$PID_FILE")
    osascript -e 'display notification "守护进程已启动 (PID: '"$NEW_PID"')" with title "HotMic" subtitle "按住 Ctrl+Alt 说话"'
else
    osascript -e 'display notification "启动失败，请查看日志" with title "HotMic" subtitle "~/.config/hotmic/daemon.log"'
fi
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/hotmic-launcher"

# ── Create DMG ─────────────────────────────────────
echo "Creating DMG..."
hdiutil create -volname "HotMic" \
    -srcfolder "$APP_DIR" \
    -ov -format UDZO \
    "$DMG_PATH" >/dev/null 2>&1

echo ""
echo "Build complete:"
echo "  App: $APP_DIR"
echo "  DMG: $DMG_PATH"
echo "  Size: $(du -h "$DMG_PATH" | cut -f1)"
