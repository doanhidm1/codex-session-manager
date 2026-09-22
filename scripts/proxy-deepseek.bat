@echo off
chcp 65001 >nul
title DeepSeek Reverse Proxy Manager

if exist "%~dp0..\codex_migrator.py" (
    set "MIGRATOR=%~dp0..\codex_migrator.py"
) else (
    set "MIGRATOR=D:\codex-session-manager\codex_migrator.py"
)

:menu
cls
echo ====================================================================
echo             CODEX DEEPSEEK REVERSE PROXY MANAGER (PORT 8765)
echo ====================================================================
echo.
python "%MIGRATOR%" proxy status
echo.
echo --------------------------------------------------------------------
echo   [1] Start Proxy (Run background daemon)
echo   [2] Stop Proxy (Terminate daemon)
echo   [3] Restart Proxy (Reload daemon)
echo   [4] Check Status (Detailed status)
echo   [5] Enable Auto-start on Windows Boot
echo   [6] Disable Auto-start on Windows Boot
echo   [0] Exit
echo --------------------------------------------------------------------
set "choice="
set /p choice="Enter choice [1-6, 0] (Default 1: Start Proxy): "

if "%choice%"=="" goto start_proxy
if "%choice%"=="1" goto start_proxy
if "%choice%"=="2" goto stop_proxy
if "%choice%"=="3" goto restart_proxy
if "%choice%"=="4" goto check_status
if "%choice%"=="5" goto enable_autostart
if "%choice%"=="6" goto disable_autostart
if "%choice%"=="0" goto exit_script
goto menu

:start_proxy
echo.
echo [*] Checking and starting DeepSeek Reverse Proxy...
python "%MIGRATOR%" proxy start
echo.
pause
goto menu

:stop_proxy
echo.
echo [*] Stopping DeepSeek Reverse Proxy...
python "%MIGRATOR%" proxy stop
echo.
pause
goto menu

:restart_proxy
echo.
echo [*] Restarting DeepSeek Reverse Proxy...
python "%MIGRATOR%" proxy restart
echo.
pause
goto menu

:check_status
echo.
python "%MIGRATOR%" proxy status
echo.
pause
goto menu

:enable_autostart
echo.
echo [*] Setting up auto-start on Windows boot (Startup)...
python "%MIGRATOR%" proxy enable-autostart
echo.
pause
goto menu

:disable_autostart
echo.
echo [*] Removing auto-start on Windows boot...
python "%MIGRATOR%" proxy disable-autostart
echo.
pause
goto menu

:exit_script
exit /b 0
