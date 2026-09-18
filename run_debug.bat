@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Answer Assistant - Debug

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   Environment not installed yet. Please run install.bat first.
    echo.
    pause
    exit /b 1
)

echo Starting in debug mode. Close this window to stop the program.
echo.
".venv\Scripts\python.exe" "main.py" --debug %*
echo.
echo Program exited with code %errorlevel%. See logs\app.log for details.
pause
