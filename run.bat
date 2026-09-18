@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Answer Assistant

if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo   Environment not installed yet. Please run install.bat first.
    echo.
    pause
    exit /b 1
)

rem pythonw = no console window. Logs go to logs\app.log
start "" ".venv\Scripts\pythonw.exe" "main.py" %*
exit /b 0
