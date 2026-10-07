@echo off
chcp 65001 >nul
setlocal
title Codex Remote Control Server + Session Bridge

echo ========================================================
echo   Starting Codex Remote Control Server with Session Bridge
echo ========================================================
echo.

:: 1. Ensure codex_session MCP is configured in ~/.codex/config.toml
echo [*] Checking and ensuring codex_session MCP is registered...
node "%~dp0ensure-session-mcp.mjs"
if errorlevel 1 (
    echo [!] Warning: ensure-session-mcp.mjs exited with an error. Continuing...
)

:: 2. Check and terminate any conflicting previous app-server on port 45000 or old instances
echo [*] Checking port 45000 and old app-server instances...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 45000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name = 'codex.exe'\" | Where-Object { $_.CommandLine -like '*app-server*' -and $_.CommandLine -like '*--listen off*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [*] Starting Codex app-server with remote-control and WebSocket bridge (port 45000)...
echo [*] Remote control enabled for web/mobile pairing.
echo [*] Session bridge active at ws://127.0.0.1:45000 (inter-session steering enabled).
echo.

codex -c features.code_mode_host=true -c analytics.enabled=false app-server --remote-control --listen ws://127.0.0.1:45000

if errorlevel 1 (
    echo.
    echo [!] Codex exited with error code %errorlevel%.
    pause
)
