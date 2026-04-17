#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/dist"
APP_DIR="$BUILD_DIR/HotMic.app"
DMG_PATH="$BUILD_DIR/HotMic.dmg"

echo "Building HotMic.app..."
rm -rf "$APP_DIR" "$DMG_PATH"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# Info.plist
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>HotMic</string>
    <key>CFBundleDisplayName</key><string>HotMic</string>
    <key>CFBundleIdentifier</key><string>com.hotmic.app</string>
    <key>CFBundleVersion</key><string>1.1.0</string>
    <key>CFBundleShortVersionString</key><string>1.1.0</string>
    <key>CFBundleExecutable</key><string>hotmic-launcher</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>LSUIElement</key><true/>
    <key>NSMicrophoneUsageDescription</key><string>HotMic needs microphone access for voice dictation.</string>
</dict>
</plist>
PLIST

# Bundle install.sh
cp "$PROJECT_DIR/install.sh" "$APP_DIR/Contents/Resources/install.sh"
chmod +x "$APP_DIR/Contents/Resources/install.sh"

# Launcher
cat > "$APP_DIR/Contents/MacOS/hotmic-launcher" <<'LAUNCHER'
#!/bin/bash
HOTMIC_DIR="$HOME/.hotmic"
APP_DIR="$HOTMIC_DIR/app"
SELF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLED_INSTALLER="$SELF_DIR/Resources/install.sh"

if [[ ! -d "$APP_DIR" || ! -f "$APP_DIR/.venv/bin/python" ]]; then
    osascript <<EOF
tell application "Terminal"
    activate
    do script "clear && echo '' && echo '  ╔══════════════════════════════════════╗' && echo '  ║   HotMic 首次安装（仅需一次）        ║' && echo '  ╚══════════════════════════════════════╝' && echo '' && bash '$BUNDLED_INSTALLER' && echo '' && echo '✅ 安装完成！请再次双击 HotMic.app 启动' && echo '   此窗口可以关闭'"
end tell
EOF
    exit 0
fi

cd "$APP_DIR"
PID_FILE="$HOME/.config/hotmic/daemon.pid"
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        osascript -e 'display notification "已在运行 — 按住 Ctrl+Alt 说话" with title "HotMic 🎙"'
        # Launch menu bar if not running
        PYTHONPATH="$APP_DIR/src" .venv/bin/python -c "
import subprocess, os
r = subprocess.run(['pgrep', '-f', 'menubar'], capture_output=True)
if r.returncode != 0:
    os.execvp('.venv/bin/python', ['.venv/bin/python', 'src/hotmic/menubar.py'])
" &
        exit 0
    fi
fi

mkdir -p "$HOME/.config/hotmic"
.venv/bin/python scripts/exec.py daemon start --background --quiet 2>>"$HOME/.config/hotmic/daemon.log"
sleep 2

if [[ -f "$PID_FILE" ]]; then
    NEW_PID=$(cat "$PID_FILE")
    osascript -e 'display notification "已启动 (PID: '"$NEW_PID"') — 按住 Ctrl+Alt 说话" with title "HotMic 🎙"'
    # Launch menu bar
    PYTHONPATH="$APP_DIR/src" nohup .venv/bin/python src/hotmic/menubar.py >/dev/null 2>&1 &
else
    osascript -e 'display notification "启动失败，请查看日志" with title "HotMic" subtitle "~/.config/hotmic/daemon.log"'
fi
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/hotmic-launcher"

# Create DMG
echo "Creating DMG..."
hdiutil create -volname "HotMic" -srcfolder "$APP_DIR" -ov -format UDZO "$DMG_PATH" >/dev/null 2>&1

echo "Done: $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
