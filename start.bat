@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Potato Bot

if not exist ".venv\Scripts\python.exe" (
    echo The bot is not set up yet. Run setup.bat first.
    pause
    exit /b 1
)

rem Restart delay in seconds. Doubles (max 300) each time the bot dies before coming
rem online, and resets to 5 after any run that did come online. No crash-loop hammering.
set /a DELAY=5

:loop
".venv\Scripts\python.exe" -X utf8 bot.py %*
set "CODE=!errorlevel!"

if "!CODE!"=="0" (
    echo Bot stopped cleanly.
    goto :end
)
if "!CODE!"=="78" (
    echo.
    echo Configuration problem - see the message above. Not restarting.
    pause
    goto :end
)
if "!CODE!"=="2" (
    echo Bot failed before coming online. Restarting in !DELAY!s. Press Ctrl+C to stop.
    timeout /t !DELAY! /nobreak >nul
    set /a DELAY=DELAY*2
    if !DELAY! gtr 300 set /a DELAY=300
    goto :loop
)
set /a DELAY=5
echo Bot exited with code !CODE!. Restarting in 5s. Press Ctrl+C to stop.
timeout /t 5 /nobreak >nul
goto :loop

:end
endlocal
