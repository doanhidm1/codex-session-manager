@echo off
chcp 65001 >nul
echo [*] Auto-syncing messages and switching provider to DEEPSEEK...
python "%~dp0..\codex_migrator.py" switch deepseek
echo.
pause
