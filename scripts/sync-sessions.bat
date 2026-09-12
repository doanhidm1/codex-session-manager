@echo off
chcp 65001 >nul
echo [*] Dang dong bo 2 chieu tat ca cac session (Grok, SaaS, Veo3)...
python "%~dp0..\codex_migrator.py" sync all
echo.
pause
