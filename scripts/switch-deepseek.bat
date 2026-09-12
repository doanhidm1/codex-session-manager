@echo off
chcp 65001 >nul
echo [*] Auto-syncing messages and switching provider focus to DEEPSEEK...
python "%~dp0..\codex_migrator.py" switch deepseek
echo.
pause
