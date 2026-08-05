@echo off
SETLOCAL EnableDelayedExpansion

echo ===================================================
echo             eko WhatsApp Voice Agent
echo ===================================================
echo.

:: Ensure we are in the script's directory
cd /d "%~dp0"

:: Check if virtual environment is valid
if not exist ".venv\Scripts\python.exe" (
    echo [WARNING] Windows virtual environment not found or broken.
    echo Recreating virtual environment and installing dependencies...
    
    :: Try to use uv if available
    where uv >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        echo [INFO] Using 'uv' to set up virtual environment...
        uv venv --python 3.11 --clear
        if !ERRORLEVEL! neq 0 exit /b !ERRORLEVEL!
        uv pip install -e ".[stt,server,agent,dev]"
        if !ERRORLEVEL! neq 0 exit /b !ERRORLEVEL!
    ) else (
        echo [INFO] Using standard 'python' to set up virtual environment...
        python -m venv .venv
        if !ERRORLEVEL! neq 0 exit /b !ERRORLEVEL!
        .venv\Scripts\pip install -e ".[stt,server,agent,dev]"
        if !ERRORLEVEL! neq 0 exit /b !ERRORLEVEL!
    )
)

:: Read command-line arguments (default to --reload)
set ARGS=%*
if "%ARGS%"=="" (
    set ARGS=--reload
)

echo Starting FastAPI webhook server...
echo Running: .venv\Scripts\python.exe tools\run_server.py %ARGS%
echo.

".venv\Scripts\python.exe" tools\run_server.py %ARGS%

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server exited with code %ERRORLEVEL%
    pause
)
