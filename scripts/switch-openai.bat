@echo off
chcp 65001 >nul
echo [*] Auto-syncing messages and switching provider to OPENAI...
python "%~dp0..\codex_migrator.py" switch openai
echo.
pause
