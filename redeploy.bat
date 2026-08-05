@echo off
REM Redeploy whatsapp-voice to the rack server (/home/deepanshu/WA_VOXO).
REM Syncs local changes, reinstalls deps, and restarts the running process.

cd /d "%~dp0"
python tools\redeploy_rack.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Redeploy failed with code %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo Redeploy finished.
pause
