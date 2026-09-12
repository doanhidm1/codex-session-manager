@echo off
chcp 65001 >nul
echo [*] Dang chuyen thanh ben PC sang che do DEEPSEEK (an cac session OpenAI goc)...
python "%~dp0..\codex_migrator.py" focus deepseek
echo.
pause
