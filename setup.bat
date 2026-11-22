@echo off
setlocal
cd /d "%~dp0"
title Potato Bot setup

echo === Potato Bot setup ===

rem ---- find Python 3.11+ ----
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [X] Python was not found.
    echo     Install Python 3.11 or newer from https://www.python.org/downloads/
    echo     and tick "Add python.exe to PATH" in the installer. Then run setup.bat again.
    goto :fail
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [X] Python 3.11 or newer is required. Found:
    %PY% --version
    goto :fail
)

rem ---- virtual environment ----
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
) else (
    echo [1/4] Virtual environment already exists.
)

echo [2/4] Installing dependencies (needs internet, about 30 MB)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet -r requirements.txt
if errorlevel 1 (
    echo [X] Dependency install failed. Check your internet connection and run setup.bat again.
    goto :fail
)

rem ---- .env ----
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [3/4] Created .env - open it in Notepad and paste your bot token.
) else (
    echo [3/4] .env already exists.
)

rem ---- helpers ----
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [!] ffmpeg not found. Music and video will not work until you install it:
    echo         winget install Gyan.FFmpeg
    echo     then close and reopen this window.
)

echo [4/4] Self-test...
".venv\Scripts\python.exe" bot.py --check
if errorlevel 1 (
    echo [!] The self-test reported a problem. Read the lines above.
) else (
    echo.
    echo Setup finished. Put your token in .env, then double-click start.bat
)
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
