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


def start_proxy_daemon(host: str = PROXY_HOST, port: int = PROXY_PORT) -> bool:
    """
    Starts the reverse proxy in the background if it's not already running.
    Returns True if proxy is running/started successfully.
    """
    if is_proxy_running(host, port):
        logger.info("DeepSeek proxy is already running on http://%s:%d", host, port)
        return True

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
