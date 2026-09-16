"""
Configuration constants for DeepSeek Reverse Proxy.
"""

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8765
DEEPSEEK_UPSTREAM = "https://api.deepseek.com"
PROXY_HEALTH_PATH = "/health"
PROXY_PING_TIMEOUT_SEC = 2.0
UPSTREAM_TIMEOUT_SEC = 300.0
UPSTREAM_CONNECT_TIMEOUT_SEC = 30.0
