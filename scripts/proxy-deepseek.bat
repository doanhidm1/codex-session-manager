@echo off
chcp 65001 >nul
title DeepSeek Reverse Proxy Manager

:menu
cls
echo ====================================================================
echo             CODEX DEEPSEEK REVERSE PROXY MANAGER (PORT 8765)
echo ====================================================================
echo.
python "D:\codex-session-manager\codex_migrator.py" proxy status
echo.
echo --------------------------------------------------------------------
echo   [1] Start Proxy (Bat daemon ngam)
echo   [2] Stop Proxy (Tat daemon)
echo   [3] Restart Proxy (Khoi dong lai daemon)
echo   [4] Kiem tra trang thai chi tiet (Status)
echo   [5] Bat tu khoi dong cung Windows (Auto-start on Boot)
echo   [6] Tat tu khoi dong cung Windows
echo   [0] Thoat
echo --------------------------------------------------------------------
set "choice="
set /p choice="Nhap lua chon [1-6, 0] (Mac dinh 1: Start Proxy): "

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
echo [*] Dang kiem tra va khoi dong DeepSeek Reverse Proxy...
python "D:\codex-session-manager\codex_migrator.py" proxy start
echo.
pause
goto menu

:stop_proxy
echo.
echo [*] Dang dung DeepSeek Reverse Proxy...
python "D:\codex-session-manager\codex_migrator.py" proxy stop
echo.
pause
goto menu

:restart_proxy
echo.
echo [*] Dang khoi dong lai DeepSeek Reverse Proxy...
python "D:\codex-session-manager\codex_migrator.py" proxy restart
echo.
pause
goto menu

:check_status
echo.
python "D:\codex-session-manager\codex_migrator.py" proxy status
echo.
pause
goto menu

:enable_autostart
echo.
echo [*] Dang thiet lap tu khoi dong cung Windows (Startup)...
python "D:\codex-session-manager\codex_migrator.py" proxy enable-autostart
echo.
pause
goto menu

:disable_autostart
echo.
echo [*] Dang go bo tu khoi dong cung Windows...
python "D:\codex-session-manager\codex_migrator.py" proxy disable-autostart
echo.
pause
goto menu

:exit_script
exit /b 0
