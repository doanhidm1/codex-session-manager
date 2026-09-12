#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_CMD="python3"

if ! command -v python3 &>/dev/null; then
    if command -v python &>/dev/null; then
        PYTHON_CMD="python"
    else
        echo "[-] Error: python3 is not installed or not in PATH."
        exit 1
    fi
fi

echo "[*] Auto-syncing messages and switching provider to DEEPSEEK..."
"$PYTHON_CMD" "$SCRIPT_DIR/../codex_migrator.py" switch deepseek
