@echo off
chcp 65001 >nul
echo [*] Running bidirectional sync across all registered session pairs...
python "%~dp0..\codex_migrator.py" sync all
echo.
pause
