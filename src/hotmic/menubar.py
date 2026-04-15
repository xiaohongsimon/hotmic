"""HotMic macOS menu bar app — status monitor + one-click fix."""

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import rumps

CONFIG_DIR = Path.home() / ".config" / "hotmic"
CONFIG_DIR_LEGACY = Path.home() / ".config" / "voice-to-claude"
PID_FILE = CONFIG_DIR / "daemon.pid"
PID_FILE_LEGACY = CONFIG_DIR_LEGACY / "daemon.pid"
LOG_FILE = CONFIG_DIR / "daemon.log"
LOG_FILE_LEGACY = CONFIG_DIR_LEGACY / "daemon.log"
WORKER_PORT = 8788


def _read_pid() -> int | None:
    for f in (PID_FILE, PID_FILE_LEGACY):
        try:
            return int(f.read_text().strip())
        except (FileNotFoundError, ValueError):
            continue
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_running() -> tuple[bool, int | None]:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        return True, pid
    return False, None


def _worker_ok() -> bool:
    """Check if ASR worker is alive via PID file (avoids blocking the single TCP connection)."""
    for d in (CONFIG_DIR, CONFIG_DIR_LEGACY):
        pid_file = d / "qwen3-worker.pid"
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, OSError):
            continue
    return False


def _mic_ok() -> tuple[bool, str]:
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        name = dev.get("name", "Unknown") if isinstance(dev, dict) else "Unknown"
        return True, name
    except Exception as e:
        return False, str(e)


def _find_exec():
    """Find the exec.py script."""
    # Check ~/.hotmic/app first (installed via install.sh)
    app_exec = Path.home() / ".hotmic" / "app" / "scripts" / "exec.py"
    if app_exec.exists():
        python = Path.home() / ".hotmic" / "app" / ".venv" / "bin" / "python"
        if python.exists():
            return str(python), str(app_exec)

    # Check plugin marketplace
    plugin_exec = Path.home() / ".claude" / "plugins" / "marketplaces" / "voice-to-claude-marketplace" / "scripts" / "exec.py"
    if plugin_exec.exists():
        plugin_python = Path.home() / ".claude" / "plugins" / "marketplaces" / "voice-to-claude-marketplace" / ".venv" / "bin" / "python"
        if plugin_python.exists():
            return str(plugin_python), str(plugin_exec)

    # Fallback: current project
    here = Path(__file__).resolve().parents[2] / "scripts" / "exec.py"
    return sys.executable, str(here)


def _run_daemon_cmd(*args) -> tuple[bool, str]:
    python, exec_py = _find_exec()
    try:
        r = subprocess.run(
            [python, exec_py, "daemon", *args],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0, r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        return False, str(e)


class HotMicApp(rumps.App):
    def __init__(self):
        super().__init__("", quit_button=None)
        self.icon = None  # use title as icon
        self.title = "🎙"

        self.status_item = rumps.MenuItem("状态: 检查中...")
        self.asr_item = rumps.MenuItem("ASR: 检查中...")
        self.mic_item = rumps.MenuItem("麦克风: 检查中...")

        self.menu = [
            self.status_item,
            self.asr_item,
            self.mic_item,
            None,
            rumps.MenuItem("▶ 启动", callback=self.on_start),
            rumps.MenuItem("⏹ 停止", callback=self.on_stop),
            None,
            rumps.MenuItem("🔧 一键修复", callback=self.on_fix),
            rumps.MenuItem("📋 查看日志", callback=self.on_log),
            None,
            rumps.MenuItem("退出", callback=self.on_quit),
        ]

        # Initial check
        self.check_health(None)

    @rumps.timer(15)
    def check_health(self, _):
        """Periodic health check — update menu items and icon color."""
        all_ok = True

        # Daemon
        running, pid = _daemon_running()
        if running:
            self.status_item.title = f"状态: 运行中 (PID: {pid})"
        else:
            self.status_item.title = "状态: 未运行"
            all_ok = False

        # ASR Worker
        if running and _worker_ok():
            self.asr_item.title = "ASR: Qwen3-ASR ✓"
        elif running:
            self.asr_item.title = "ASR: 未就绪 ✗"
            all_ok = False
        else:
            self.asr_item.title = "ASR: —"

        # Microphone
        mic_ok, mic_name = _mic_ok()
        if mic_ok:
            self.mic_item.title = f"麦克风: {mic_name} ✓"
        else:
            self.mic_item.title = "麦克风: 不可用 ✗"
            all_ok = False

        # Update icon
        self.title = "🎙" if all_ok else "🔴"

    def on_start(self, _):
        running, _ = _daemon_running()
        if running:
            rumps.notification("HotMic", "", "守护进程已在运行")
            return
        rumps.notification("HotMic", "", "正在启动...")
        ok, msg = _run_daemon_cmd("start", "--background", "--quiet")
        if ok:
            rumps.notification("HotMic", "启动成功", "按住 Ctrl+Alt 说话")
        else:
            rumps.notification("HotMic", "启动失败", msg[:100])
        self.check_health(None)

    def on_stop(self, _):
        running, _ = _daemon_running()
        if not running:
            rumps.notification("HotMic", "", "守护进程未在运行")
            return
        ok, msg = _run_daemon_cmd("stop")
        rumps.notification("HotMic", "已停止" if ok else "停止失败", msg[:100] if not ok else "")
        # Kill worker too
        try:
            s = socket.create_connection(("127.0.0.1", WORKER_PORT), timeout=2)
            import struct, json
            cmd = json.dumps({"cmd": "shutdown"}).encode()
            s.sendall(struct.pack(">I", len(cmd)) + cmd)
            s.close()
        except Exception:
            pass
        self.check_health(None)

    def on_fix(self, _):
        """One-click fix: stop everything, restart fresh."""
        rumps.notification("HotMic", "修复中...", "正在重启所有组件")

        # Stop daemon
        _run_daemon_cmd("stop")

        # Kill any orphan worker
        try:
            subprocess.run(["lsof", "-ti", f":{WORKER_PORT}"], capture_output=True, text=True, timeout=5)
            os.system(f"lsof -ti :{WORKER_PORT} | xargs kill 2>/dev/null")
        except Exception:
            pass

        # Clean stale PID files
        PID_FILE.unlink(missing_ok=True)
        (CONFIG_DIR / "qwen3-worker.pid").unlink(missing_ok=True)

        import time
        time.sleep(1)

        # Restart
        ok, msg = _run_daemon_cmd("start", "--background", "--quiet")

        import time
        time.sleep(3)

        # Verify
        running, pid = _daemon_running()
        worker = _worker_ok()
        mic, mic_name = _mic_ok()

        if running and worker and mic:
            rumps.notification("HotMic", "修复成功 ✓", f"PID: {pid} | 麦克风: {mic_name}")
        else:
            issues = []
            if not running:
                issues.append("守护进程未启动")
            if not worker:
                issues.append("ASR Worker 未就绪")
            if not mic:
                issues.append("麦克风不可用")
            rumps.notification("HotMic", "部分修复", " | ".join(issues))

        self.check_health(None)

    def on_log(self, _):
        """Open log in Console.app."""
        for f in (LOG_FILE, LOG_FILE_LEGACY):
            if f.exists():
                subprocess.Popen(["open", "-a", "Console", str(f)])
                return
        rumps.notification("HotMic", "", "日志文件不存在")

    def on_quit(self, _):
        # Stop daemon before quitting
        running, _ = _daemon_running()
        if running:
            _run_daemon_cmd("stop")
        rumps.quit_application()


def main():
    HotMicApp().run()


if __name__ == "__main__":
    main()
