@echo off
chcp 65001 >nul
echo [*] Dang tu dong dong bo tin nhan va chuyen sang che do DEEPSEEK...
python "%~dp0..\codex_migrator.py" switch deepseek
echo.
pause
