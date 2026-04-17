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


def _overlay_ok() -> bool:
    """Check if overlay process is alive by trying to bind its port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 19876))
        s.close()
        return False  # port was free — overlay not running
    except OSError:
        return True  # port bound — overlay is running


def _mic_ok() -> tuple[bool, str]:
    try:
        import sounddevice as sd
        sd._terminate()  # force refresh device cache
        sd._initialize()
        dev = sd.query_devices(kind="input")
        name = dev.get("name", "Unknown") if isinstance(dev, dict) else "Unknown"
        return True, name
    except Exception as e:
        return False, str(e)


def _find_exec():
    """Find the exec.py script. Prefer marketplace (latest code) over ~/.hotmic/app."""
    # Check plugin marketplace first (most likely to have latest code)
    plugin_exec = Path.home() / ".claude" / "plugins" / "marketplaces" / "voice-to-claude-marketplace" / "scripts" / "exec.py"
    if plugin_exec.exists():
        plugin_python = Path.home() / ".claude" / "plugins" / "marketplaces" / "voice-to-claude-marketplace" / ".venv" / "bin" / "python"
        if plugin_python.exists():
            return str(plugin_python), str(plugin_exec)

    # Fallback: ~/.hotmic/app (installed via install.sh)
    app_exec = Path.home() / ".hotmic" / "app" / "scripts" / "exec.py"
    if app_exec.exists():
        python = Path.home() / ".hotmic" / "app" / ".venv" / "bin" / "python"
        if python.exists():
            return str(python), str(app_exec)

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

        self.toggle_item = rumps.MenuItem("▶ 启动", callback=self.on_toggle)
        self.menu = [
            self.status_item,
            self.asr_item,
            self.mic_item,
            None,
            self.toggle_item,
            rumps.MenuItem("🔧 一键修复（遇到问题点这里）", callback=self.on_fix),
            None,
            rumps.MenuItem("📋 查看日志", callback=self.on_log),
            rumps.MenuItem("退出", callback=self.on_quit),
        ]

        # Initial check
        self.check_health(None)

    @rumps.timer(15)
    def check_health(self, _):
        """Periodic health check — update menu items + auto-recover on persistent issues."""
        if not hasattr(self, '_fail_count'):
            self._fail_count = 0
            self._auto_fixing = False

        all_ok = True
        daemon_issue = False
        worker_issue = False

        # Daemon
        running, pid = _daemon_running()
        if running:
            self.status_item.title = f"状态: 运行中 (PID: {pid})"
        else:
            self.status_item.title = "状态: 未运行"
            all_ok = False
            daemon_issue = True

        # ASR Worker (only relevant when daemon running)
        if running and _worker_ok():
            self.asr_item.title = "ASR: Qwen3-ASR ✓"
        elif running:
            self.asr_item.title = "ASR: 未就绪 ✗"
            all_ok = False
            worker_issue = True
        else:
            self.asr_item.title = "ASR: —"

        # Microphone
        mic_ok, mic_name = _mic_ok()
        if mic_ok:
            self.mic_item.title = f"麦克风: {mic_name} ✓"
        else:
            self.mic_item.title = "麦克风: 不可用 ✗"
            all_ok = False

        # Update icon and toggle label
        self.title = "🎙" if all_ok else "🔴"
        self.toggle_item.title = "⏹ 停止" if running else "▶ 启动"

        # Mic error sentinel: daemon writes this when PortAudio is corrupted
        mic_error_flag = CONFIG_DIR / "mic_error.flag"
        mic_error_flag_legacy = CONFIG_DIR_LEGACY / "mic_error.flag"
        if mic_error_flag.exists() or mic_error_flag_legacy.exists():
            for f in (mic_error_flag, mic_error_flag_legacy):
                f.unlink(missing_ok=True)
            if not self._auto_fixing:
                self._auto_fixing = True
                import threading
                threading.Thread(target=self._auto_recover, daemon=True).start()
            return

        # Overlay check: if daemon running but overlay missing → auto-recover
        overlay_issue = running and not _overlay_ok()

        # Auto-recovery: daemon/worker/overlay unhealthy for 2 consecutive checks (30s)
        if running and (daemon_issue or worker_issue or overlay_issue):
            self._fail_count += 1
        else:
            self._fail_count = 0

        if self._fail_count >= 2 and not self._auto_fixing:
            self._auto_fixing = True
            self._fail_count = 0
            import threading
            threading.Thread(target=self._auto_recover, daemon=True).start()

    def _auto_recover(self):
        """Background auto-recovery — silent clean restart."""
        try:
            import time
            rumps.notification("HotMic", "自动修复中", "检测到异常，正在重启")
            self._clean_all_processes()
            time.sleep(1)
            _run_daemon_cmd("start", "--background", "--quiet")
            time.sleep(3)
            running, _ = _daemon_running()
            if running and _worker_ok():
                rumps.notification("HotMic", "自动修复成功", "一切正常")
            else:
                rumps.notification("HotMic", "自动修复失败", "请点击一键修复")
        finally:
            self._auto_fixing = False

    def on_toggle(self, _):
        """Start or stop daemon based on current state."""
        running, _ = _daemon_running()
        if running:
            self.on_stop(_)
        else:
            self.on_start(_)

    def on_start(self, _):
        """Clicking 启动 always does a clean restart — kills any stale processes first.
        Matches user mental model: "click start = fresh environment"."""
        import time
        rumps.notification("HotMic", "", "正在启动（清理 + 重启）...")
        # Full clean restart: kill everything, then start fresh
        self._clean_all_processes()
        time.sleep(0.8)
        ok, msg = _run_daemon_cmd("start", "--background", "--quiet")
        time.sleep(2)
        if ok:
            running, pid = _daemon_running()
            if running:
                rumps.notification("HotMic", "启动成功", f"PID: {pid} | 按住 Ctrl+Alt 说话")
            else:
                rumps.notification("HotMic", "启动异常", "守护进程未就绪")
        else:
            rumps.notification("HotMic", "启动失败", msg[:100])
        self.check_health(None)

    def _clean_all_processes(self):
        """Kill ALL hotmic-related processes (any install path) and free ports."""
        import subprocess
        try:
            _run_daemon_cmd("stop")
        except Exception:
            pass
        # Kill by pattern — catches all install paths (~/.hotmic/app, marketplace, dev)
        for pattern in ("exec.py daemon", "qwen3_asr_worker", "_overlay_process"):
            try:
                subprocess.run(["pkill", "-9", "-f", pattern], timeout=3, capture_output=True)
            except Exception:
                pass
        for port in (WORKER_PORT, 19876):
            try:
                r = subprocess.run(["lsof", "-ti", f":{port}"], timeout=3, capture_output=True, text=True)
                for pid in r.stdout.strip().split():
                    try:
                        os.kill(int(pid), 9)
                    except Exception:
                        pass
            except Exception:
                pass
        # Clean stale PID files
        for d in (CONFIG_DIR, CONFIG_DIR_LEGACY):
            for f in ("daemon.pid", "qwen3-worker.pid"):
                (d / f).unlink(missing_ok=True)

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
        """One-click fix: kills everything, restarts fresh, verifies."""
        import time
        rumps.notification("HotMic", "修复中...", "正在重启所有组件")
        self._clean_all_processes()
        time.sleep(1)
        ok, msg = _run_daemon_cmd("start", "--background", "--quiet")
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
