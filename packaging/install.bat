@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Answer Assistant - Offline Setup

echo.
echo ============================================================
echo    Answer Assistant   ^|   Offline Setup
echo ============================================================
echo.
echo   This package is self-contained:
echo     - portable Python runtime with tkinter     (runtime\)
echo     - preinstalled dependencies                (.venv\)
echo     - OCR models                               (models\)
echo   No internet connection is required.
echo.

if not exist "runtime\python\python.exe" (
    echo   [ERROR] runtime\python\python.exe not found.
    echo.
    echo   Please extract the WHOLE archive to a folder on your disk
    echo   before running this script. Do not run it from inside the zip.
    echo.
    pause
    exit /b 1
)

if not exist "tools\setup_env.py" (
    echo   [ERROR] tools\setup_env.py not found. The package is incomplete.
    echo.
    pause
    exit /b 1
)

echo [1/2] Configuring the bundled environment...
echo.
"runtime\python\python.exe" "tools\setup_env.py"
if errorlevel 1 (
    echo.
    echo   [ERROR] Setup failed. See the messages above.
    echo.
    pause
    exit /b 1
)

echo [2/2] Running self-check...
echo.
".venv\Scripts\python.exe" "tools\selfcheck.py" --quick
set "SC=%errorlevel%"

echo.
echo ============================================================
if "%SC%"=="0" (
    echo    Setup finished successfully.
) else (
    echo    Setup finished, but the self-check reported failures above.
)
echo ============================================================
echo.
echo   How to start:
echo     - run.bat          normal start ^(no console window^)
echo     - run_debug.bat    start with a log window, for troubleshooting
echo.
echo   How to use:
echo     1. Import your question bank in the control panel
echo     2. Press F1 and drag to select the question area
echo     3. Press F2 to start recognizing; the panel minimizes itself
echo     4. Press F9, or click X on the capture frame, to stop
echo.
pause
exit /b 0
