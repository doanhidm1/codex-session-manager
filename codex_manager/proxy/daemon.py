"""
Process lifecycle and health management for DeepSeek Reverse Proxy.
"""

import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict

import httpx

from .config import PROXY_HEALTH_PATH, PROXY_HOST, PROXY_PING_TIMEOUT_SEC, PROXY_PORT

logger = logging.getLogger("deepseek_proxy.daemon")


def is_proxy_running(host: str = PROXY_HOST, port: int = PROXY_PORT) -> bool:
    """Checks if the proxy is running and responding to health check."""
    url = f"http://{host}:{port}{PROXY_HEALTH_PATH}"
    try:
        resp = httpx.get(url, timeout=PROXY_PING_TIMEOUT_SEC)
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def get_proxy_status(host: str = PROXY_HOST, port: int = PROXY_PORT) -> Dict[str, Any]:
    """Returns detailed status information about the proxy."""
    running = is_proxy_running(host, port)
    url = f"http://{host}:{port}"
    return {
        "running": running,
        "host": host,
        "port": port,
        "url": url,
        "upstream": "https://api.deepseek.com",
    }


SYSTEMD_UNIT = "codex-deepseek-proxy.service"


def _systemd_unit_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user", SYSTEMD_UNIT)


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def _use_systemd() -> bool:
    return sys.platform.startswith("linux") and os.path.exists(_systemd_unit_path())


def _wait_until(predicate, timeout_sec: float = 5.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def start_proxy_daemon(host: str = PROXY_HOST, port: int = PROXY_PORT) -> bool:
    """
    Starts the reverse proxy in the background if it's not already running.
    Returns True if proxy is running/started successfully.
    """
    if is_proxy_running(host, port):
        logger.info("DeepSeek proxy is already running on http://%s:%d", host, port)
        return True

    if _use_systemd():
        # Supervised by systemd: survives the terminal/session that ran the switch and restarts on crash
        _systemctl("start", SYSTEMD_UNIT)
        return _wait_until(lambda: is_proxy_running(host, port))

    logger.info("Starting DeepSeek proxy daemon on http://%s:%d...", host, port)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proxy_script = os.path.join(repo_root, "codex_manager", "deepseek_proxy.py")

    env = os.environ.copy()
    env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")

    # Launch detached background process on Windows/Unix
    kwargs: Dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": repo_root,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        # Detach from the caller's session so closing the terminal (or the agent shell) does not kill the proxy
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([sys.executable, proxy_script, str(port)], **kwargs)
    except Exception as e:
        logger.error("Failed to spawn proxy daemon: %s", e)
        return False

    # Wait up to 5 seconds for health check
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if is_proxy_running(host, port):
            logger.info("DeepSeek proxy daemon started successfully.")
            return True
        time.sleep(0.2)

    logger.warning("Proxy spawned but health check did not pass within 5s.")
    return False


def stop_proxy_daemon(host: str = PROXY_HOST, port: int = PROXY_PORT) -> bool:
    """
    Requests the proxy to gracefully shut down via admin endpoint.
    Returns True if stopped.
    """
    if _use_systemd():
        # Stop through the unit, otherwise Restart= would bring the proxy back
        _systemctl("stop", SYSTEMD_UNIT)
        return _wait_until(lambda: not is_proxy_running(host, port), 3.0)

    if not is_proxy_running(host, port):
        logger.info("Proxy is not running.")
        return True

    url = f"http://{host}:{port}/shutdown"
    try:
        httpx.post(url, timeout=2.0)
    except Exception:
        pass

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not is_proxy_running(host, port):
            logger.info("Proxy stopped successfully.")
            return True
        time.sleep(0.2)

    return not is_proxy_running(host, port)


def _enable_systemd_autostart() -> bool:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proxy_script = os.path.join(repo_root, "codex_manager", "deepseek_proxy.py")
    unit = f"""[Unit]
Description=Codex DeepSeek reverse proxy (127.0.0.1:{PROXY_PORT})
After=network-online.target

[Service]
Type=simple
WorkingDirectory={repo_root}
Environment=PYTHONPATH={repo_root}
ExecStart={sys.executable} {proxy_script} {PROXY_PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    os.makedirs(os.path.dirname(_systemd_unit_path()), exist_ok=True)
    with open(_systemd_unit_path(), "w", encoding="utf-8") as f:
        f.write(unit)
    # Hand a manually started proxy over to systemd
    if is_proxy_running() and not _systemctl("is-active", "--quiet", SYSTEMD_UNIT):
        try:
            httpx.post(f"http://{PROXY_HOST}:{PROXY_PORT}/shutdown", timeout=2.0)
        except Exception:
            pass
        _wait_until(lambda: not is_proxy_running(), 3.0)
    return _systemctl("daemon-reload") and _systemctl("enable", "--now", SYSTEMD_UNIT)


def enable_autostart() -> bool:
    """Configures the proxy to start automatically (Windows Startup shortcut, or a systemd user unit on Linux)."""
    if sys.platform.startswith("linux"):
        return _enable_systemd_autostart()
    if sys.platform != "win32":
        return False
    startup_dir = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    if not os.path.exists(startup_dir):
        return False
    shortcut_path = os.path.join(startup_dir, "Start-DeepSeek-Proxy.lnk")
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proxy_script = os.path.join(repo_root, "codex_manager", "deepseek_proxy.py")
    try:
        ps_script = (
            f"$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{shortcut_path}'); "
            f"$s.TargetPath = 'pythonw.exe'; "
            f"$s.Arguments = '\"{proxy_script}\"'; "
            f"$s.WorkingDirectory = '{repo_root}'; "
            f"$s.WindowStyle = 7; "
            f"$s.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            check=True,
            capture_output=True,
        )
        return True
    except Exception as e:
        logger.error("Failed to enable autostart: %s", e)
        return False


def disable_autostart() -> bool:
    """Removes the proxy autostart (Windows Startup shortcut, or the systemd user unit on Linux)."""
    if sys.platform.startswith("linux"):
        # Only drop the login autostart; the unit stays so start/stop remain supervised and the proxy keeps running
        return _systemctl("disable", SYSTEMD_UNIT)
    if sys.platform != "win32":
        return False
    startup_dir = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    shortcut_path = os.path.join(startup_dir, "Start-DeepSeek-Proxy.lnk")
    try:
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)
        return True
    except Exception as e:
        logger.error("Failed to disable autostart: %s", e)
        return False
