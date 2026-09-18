@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Answer Assistant - Build

if not exist ".venv\Scripts\python.exe" (
    echo   Environment not installed yet. Please run install.bat first.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo    Answer Assistant   ^|   Build executable
echo ============================================================
echo.
echo   Notes:
echo     - PaddleOCR ships many native libraries and models, the output
echo       folder will be around 1.5-2GB.
echo     - Because of that we always use --onedir, never --onefile.
echo     - In most cases copying the whole folder and using run.bat is
echo       simpler than building an exe.
echo.
echo   Press any key to start, Ctrl+C to cancel...
pause >nul

set "VPY=%~dp0.venv\Scripts\python.exe"

echo.
echo [1/3] Installing PyInstaller...
"%VPY%" -m pip install -q -U pyinstaller --disable-pip-version-check
if errorlevel 1 (
    echo   Failed to install PyInstaller.
    pause
    exit /b 1
)

echo.
echo [2/3] Generating entry script...
> _entry.py echo import multiprocessing as mp
>> _entry.py echo import sys, os
>> _entry.py echo sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
>> _entry.py echo if __name__ == "__main__":
>> _entry.py echo     mp.freeze_support()
>> _entry.py echo     from main import main
>> _entry.py echo     raise SystemExit(main())

echo.
echo [3/3] Building. This takes more than 10 minutes, please wait...
echo.

"%VPY%" -m PyInstaller ^
  --noconfirm --clean --onedir --windowed ^
  --name AnswerAssistant ^
  --distpath "dist" --workpath "build" --specpath "build" ^
  --collect-all paddleocr ^
  --collect-all paddlex ^
  --collect-all rapidocr ^
  --collect-all omegaconf ^
  --collect-all modelscope ^
  --hidden-import mss.windows ^
  --hidden-import rapidfuzz.fuzz ^
  --hidden-import docx ^
  --hidden-import openpyxl ^
  --hidden-import keyboard ^
  --add-data "tools;tools" ^
  --exclude-module matplotlib ^
  --exclude-module IPython ^
  --exclude-module pytest ^
  _entry.py

set "RC=%errorlevel%"
del /q _entry.py >nul 2>&1

echo.
echo ============================================================
if "%RC%"=="0" (
    echo    Build finished: dist\AnswerAssistant\AnswerAssistant.exe
    echo.
    echo    The OCR models are still downloaded on first run,
    echo    unless you ship the cache folder %%USERPROFILE%%\.paddlex too.
) else (
    echo    Build failed ^(exit code %RC%^). Check the messages above.
)
echo ============================================================
pause
exit /b %RC%
