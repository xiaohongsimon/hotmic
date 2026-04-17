#!/bin/bash
set -e

# HotMic Installer
# Usage: curl -sSL https://raw.githubusercontent.com/xiaohongsimon/hotmic/main/install.sh | bash

HOTMIC_DIR="$HOME/.hotmic"
VENV_DIR="$HOTMIC_DIR/venv"
REPO_DIR="$HOTMIC_DIR/app"
WHISPER_DIR="$HOTMIC_DIR/whisper.cpp"
CONFIG_DIR="$HOME/.config/hotmic"
REPO_URL="https://github.com/xiaohongsimon/hotmic.git"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[HotMic]${NC} $1"; }
warn()  { echo -e "${YELLOW}[HotMic]${NC} $1"; }
error() { echo -e "${RED}[HotMic]${NC} $1"; exit 1; }

# ── Preflight ──────────────────────────────────────

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   HotMic — macOS 本地语音输入工具     ║"
echo "  ║   按住热键说话，松开自动粘贴          ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Check macOS
[[ "$(uname)" == "Darwin" ]] || error "HotMic 仅支持 macOS"

# Check Apple Silicon
if [[ "$(uname -m)" != "arm64" ]]; then
    warn "检测到 Intel Mac，Qwen3-ASR (MLX) 需要 Apple Silicon"
    warn "将仅使用 whisper.cpp 作为 ASR 引擎"
fi

# Check Python
PYTHON=""
for p in python3.12 python3.11 python3.10 python3; do
    if command -v "$p" &>/dev/null; then
        ver=$("$p" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major="${ver%%.*}"
        minor="${ver##*.}"
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON="$p"
            break
        fi
    fi
done
[[ -n "$PYTHON" ]] || error "需要 Python 3.10+。请安装：brew install python@3.12"
info "Python: $PYTHON ($($PYTHON --version))"

# Check cmake
if ! command -v cmake &>/dev/null; then
    warn "未找到 cmake，正在安装..."
    if command -v brew &>/dev/null; then
        brew install cmake
    else
        error "需要 cmake。请安装：brew install cmake"
    fi
fi

# Check Xcode CLI tools
if ! xcode-select -p &>/dev/null; then
    info "安装 Xcode Command Line Tools..."
    xcode-select --install
    echo "请在弹窗中点击安装，完成后重新运行此脚本。"
    exit 0
fi

# ── Step 1: Clone/Update repo ──────────────────────

info "[1/5] 下载 HotMic..."
mkdir -p "$HOTMIC_DIR"

if [[ -d "$REPO_DIR/.git" ]]; then
    info "更新已有安装..."
    cd "$REPO_DIR" && git pull --quiet
else
    rm -rf "$REPO_DIR"
    git clone --quiet "$REPO_URL" "$REPO_DIR"
fi

# ── Step 2: Install Python dependencies ────────────

info "[2/5] 安装 Python 依赖..."
cd "$REPO_DIR"

if [[ ! -d ".venv" ]]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/pip install -q -e . 2>/dev/null

# ── Step 3: Setup Qwen3-ASR ───────────────────────

if [[ "$(uname -m)" == "arm64" ]]; then
    info "[3/5] 安装 Qwen3-ASR（Apple Silicon 加速）..."
    if [[ ! -d "$VENV_DIR" ]]; then
        "$PYTHON" -m venv "$VENV_DIR"
    fi
    "$VENV_DIR/bin/pip" install -q mlx-qwen3-asr 2>/dev/null

    info "预下载 Qwen3-ASR-1.7B 模型（约 3GB，请耐心等待）..."
    "$VENV_DIR/bin/python" -c "
from mlx_qwen3_asr import Session
s = Session(model='Qwen/Qwen3-ASR-1.7B')
print('Model ready')
" 2>/dev/null && info "模型下载完成" || warn "模型将在首次使用时下载"
else
    info "[3/5] 跳过 Qwen3-ASR（需要 Apple Silicon）"
fi

# ── Step 4: Build whisper.cpp ──────────────────────

info "[4/5] 构建 whisper.cpp（备用引擎）..."
if [[ -f "$WHISPER_DIR/build/bin/whisper-cli" ]]; then
    info "whisper.cpp 已构建"
else
    if [[ ! -d "$WHISPER_DIR" ]]; then
        git clone --quiet https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
    fi
    cd "$WHISPER_DIR"
    cmake -B build -DGGML_METAL=ON -DCMAKE_VERBOSE_MAKEFILE=OFF >/dev/null 2>&1
    cmake --build build -j >/dev/null 2>&1

    if [[ -f "build/bin/whisper-cli" ]]; then
        info "whisper.cpp 构建成功"
        # Download base model
        ./models/download-ggml-model.sh base >/dev/null 2>&1 && info "Whisper base 模型下载完成" || true
    else
        warn "whisper.cpp 构建失败，将仅使用 Qwen3-ASR"
    fi
fi

# ── Step 5: Configure ─────────────────────────────

info "[5/5] 配置..."
mkdir -p "$CONFIG_DIR"

CONFIG_FILE="$CONFIG_DIR/config.json"
if [[ ! -f "$CONFIG_FILE" ]]; then
    cat > "$CONFIG_FILE" <<CONF
{
  "whisper_cpp_path": "$WHISPER_DIR/build/bin/whisper-cli",
  "models_dir": "$WHISPER_DIR/models",
  "model": "base",
  "hotkey_ctrl": true,
  "hotkey_alt": true,
  "output_mode": "clipboard",
  "language": "zh",
  "streaming_mode": true,
  "overlay_enabled": true,
  "sound_effects": true,
  "max_recording_seconds": 60,
  "setup_complete": true
}
CONF
    info "配置已创建"
else
    info "已有配置，跳过"
fi

# ── Create launcher script ─────────────────────────

LAUNCHER="$HOTMIC_DIR/hotmic"
cat > "$LAUNCHER" <<'SCRIPT'
#!/bin/bash
cd "$HOME/.hotmic/app"
exec .venv/bin/python scripts/exec.py "$@"
SCRIPT
chmod +x "$LAUNCHER"

# Add to PATH hint
if ! echo "$PATH" | grep -q "$HOTMIC_DIR"; then
    SHELL_RC="$HOME/.zshrc"
    [[ "$SHELL" == */bash ]] && SHELL_RC="$HOME/.bashrc"
    if ! grep -q "hotmic" "$SHELL_RC" 2>/dev/null; then
        echo "export PATH=\"$HOTMIC_DIR:\$PATH\"" >> "$SHELL_RC"
    fi
fi

# ── Done ───────────────────────────────────────────

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║          安装完成！                   ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
info "启动："
echo "  hotmic daemon start --background"
echo ""
info "或重新打开终端后："
echo "  hotmic daemon start --background"
echo ""
info "使用："
echo "  按住 Ctrl+Alt 说话，松开自动粘贴"
echo ""
info "健康检查："
echo "  hotmic daemon health"
echo ""
warn "首次使用请授权："
echo "  系统设置 → 隐私与安全 → 麦克风 → 允许终端"
echo "  系统设置 → 隐私与安全 → 辅助功能 → 允许终端"
echo ""
