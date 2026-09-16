"""
DeepSeek Protocol Adapter Proxy for Codex Desktop.
Legacy entry point wrapping codex_manager.proxy.
"""

import os
import sys

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.proxy.adapter import adapt_responses_body
from codex_manager.proxy.config import DEEPSEEK_UPSTREAM, PROXY_HOST, PROXY_PORT
from codex_manager.proxy.daemon import (
    get_proxy_status,
    is_proxy_running,
    start_proxy_daemon,
    stop_proxy_daemon,
)
from codex_manager.proxy.reconciler import reconcile_delegation_turns
from codex_manager.proxy.server import app, main

__all__ = [
    "app",
    "main",
    "adapt_responses_body",
    "reconcile_delegation_turns",
    "is_proxy_running",
    "start_proxy_daemon",
    "stop_proxy_daemon",
    "get_proxy_status",
    "PROXY_HOST",
    "PROXY_PORT",
    "DEEPSEEK_UPSTREAM",
]

if __name__ == "__main__":
    main()
