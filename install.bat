@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Answer Assistant - Setup

echo.
echo ============================================================
echo    Answer Assistant   ^|   Environment Setup
echo ============================================================
echo.
echo   This script will:
echo     1. Prepare a Python runtime (downloads a portable one if needed)
echo     2. Create an isolated virtual environment .venv
echo     3. Install dependencies and OCR engines
echo     4. Run a self-check
echo.
echo   First install downloads about 400MB. Keep the network up.
echo.

set "PYCMD="
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

rem ==========================================================
rem  1. Python runtime
rem ==========================================================
echo [1/5] Looking for a Python runtime...

if exist "%~dp0runtime\python\python.exe" (
    set "PYCMD="runtime\python\python.exe""
    echo       Using bundled runtime: runtime\python
    goto :have_python
)

for %%V in (python3.13 python3.12 python3.11 python3.10 python3.9 python3 python) do (
    if not defined PYCMD (
        where %%V >nul 2>&1 && set "PYCMD=%%V"
    )
)

if defined PYCMD (
    %PYCMD% -c "import sys,tkinter; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo       Found Python but it lacks tkinter or is too old. Using portable runtime instead.
        set "PYCMD="
    ) else (
        echo       Using local Python:
        %PYCMD% -c "import sys;print('       '+sys.executable+'  '+sys.version.split()[0])"
    )
)

if not defined PYCMD (
    echo.
    echo [2/5] No usable Python found. Downloading portable runtime ^(about 45MB^)...
    echo.
    if not exist "runtime" mkdir "runtime"

    set "OK="

    set "URL=https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/LatestRelease/cpython-3.13.15%%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
    call :download "!URL!" "runtime\pbs.tar.gz" && set "OK=1"

    if not defined OK (
        set "URL=https://ghfast.top/https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
        call :download "!URL!" "runtime\pbs.tar.gz" && set "OK=1"
    )

    if not defined OK (
        echo.
        echo   [ERROR] Failed to download the portable runtime.
        echo.
        echo   Please download this file manually with a browser,
        echo   put it into the "runtime" folder and rename it to pbs.tar.gz :
        echo.
        echo     https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/LatestRelease/
        echo.
        echo   Then run install.bat again.
        echo.
        pause
        exit /b 1
    )

    echo       Extracting runtime...
    tar -xzf "runtime\pbs.tar.gz" -C "runtime"
    if errorlevel 1 (
        echo   [ERROR] Extraction failed. "tar" is required ^(Windows 10 1803+ has it^).
        pause
        exit /b 1
    )
    del /q "runtime\pbs.tar.gz" >nul 2>&1
    set "PYCMD="runtime\python\python.exe""
    echo       Portable runtime ready: runtime\python
)

:have_python

rem ==========================================================
rem  2. Virtual environment
rem ==========================================================
echo.
echo [3/5] Creating virtual environment .venv ...
if exist ".venv\Scripts\python.exe" (
    echo       Already exists, skipping
) else (
    %PYCMD% -m venv ".venv"
    if errorlevel 1 (
        echo   [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo       Created
)

set "VPY=%~dp0.venv\Scripts\python.exe"

"%VPY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [ERROR] tkinter is missing in the virtual environment, the GUI cannot start.
    echo   Please delete the "runtime" and ".venv" folders, then run install.bat again,
    echo   or install the official Python from python.org ^(keep the tcl/tk option checked^).
    echo.
    pause
    exit /b 1
)

rem ==========================================================
rem  3. Dependencies
rem ==========================================================
echo.
echo [4/5] Installing dependencies. This is the slow part ^(3-8 minutes^)...
echo.

"%VPY%" -m pip install --upgrade pip -q --disable-pip-version-check

echo       Installing OCR engines ^(PaddleOCR + RapidOCR^)...
"%VPY%" -m pip install -r "requirements-ocr.txt" -i %MIRROR% --disable-pip-version-check
if errorlevel 1 (
    echo       Mirror failed, retrying with the official index...
    "%VPY%" -m pip install -r "requirements-ocr.txt" --disable-pip-version-check
)

echo       Installing core dependencies...
"%VPY%" -m pip install -r "requirements.txt" -i %MIRROR% --disable-pip-version-check
if errorlevel 1 (
    echo       Mirror failed, retrying with the official index...
    "%VPY%" -m pip install -r "requirements.txt" --disable-pip-version-check
)

rem ==========================================================
rem  4. Self-check
rem ==========================================================
echo.
echo [5/5] Running self-check...
echo.
"%VPY%" "tools\selfcheck.py" --quick
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
echo     1. Move the answer overlay away from the area you want to scan
echo     2. Press F1 and drag to select the question area
echo     3. Import your question bank in the control panel, or click the
echo        sample-bank button to try it out right away
echo     4. Press F2 to start recognizing
echo.
pause
exit /b 0

rem ==========================================================
rem  Subroutine: download with curl, fallback to PowerShell
rem ==========================================================
:download
setlocal
set "U=%~1"
set "O=%~2"
if exist "%O%" del /q "%O%" >nul 2>&1

curl -L --fail --retry 2 --connect-timeout 20 -o "%O%" "%U%" >nul 2>&1
if exist "%O%" (
    for %%F in ("%O%") do if %%~zF GTR 1000000 ( endlocal & exit /b 0 )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -Uri '%U%' -OutFile '%O%' -UseBasicParsing -TimeoutSec 300}catch{exit 1}" >nul 2>&1
if exist "%O%" (
    for %%F in ("%O%") do if %%~zF GTR 1000000 ( endlocal & exit /b 0 )
)

endlocal & exit /b 1
