@echo off
chcp 65001 >nul
echo [*] Dang chuyen thanh ben PC sang che do OPENAI (an cac session DeepSeek ds)...
python "%~dp0..\codex_migrator.py" focus openai
echo.
pause
