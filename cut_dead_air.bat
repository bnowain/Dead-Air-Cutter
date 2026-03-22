@echo off
:: ============================================================
::  Dead Air Cutter — drag a video file onto this batch file
::  to automatically remove silent/frozen dead air sections.
::
::  Output:  <filename>_trimmed.mp4  in the same folder
::
::  Requirements:
::    - Python 3.x on PATH  (or edit PYTHON below)
::    - ffmpeg on PATH       (or set FFMPEG_BIN env var)
:: ============================================================

setlocal

:: ---- Configuration -----------------------------------------
:: Path to Python executable. Defaults to whatever is on PATH.
:: To use a specific venv, set the full path, e.g.:
::   set PYTHON=E:\0-Automated-Apps\civic_media\venv\Scripts\python.exe
set PYTHON=python

:: Path to this script's directory (so it works from anywhere)
set SCRIPT_DIR=%~dp0

:: Silence threshold in dB  (-50 = catches closed-session HVAC hum)
set NOISE_DB=-50

:: Minimum silence duration in seconds before a gap is cut
set MIN_SILENCE=30

:: Uncomment to disable GPU acceleration:
:: set NO_GPU=--no-gpu

:: Uncomment to cut on silence alone (skip freeze detection):
:: set AUDIO_ONLY=--audio-only
:: ------------------------------------------------------------

if "%~1"=="" (
    echo.
    echo  Dead Air Cutter
    echo  ---------------
    echo  Drag a video file onto this batch file to trim dead air.
    echo.
    echo  Or run from the command line:
    echo    cut_dead_air.bat "path\to\video.mp4"
    echo.
    pause
    exit /b 0
)

echo.
echo  Dead Air Cutter
echo  Input: %~1
echo.

%PYTHON% "%SCRIPT_DIR%cut_dead_air.py" %1 --noise-db %NOISE_DB% --min-silence %MIN_SILENCE% %NO_GPU% %AUDIO_ONLY%

if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Processing failed. See output above.
    echo.
    pause
    exit /b %errorlevel%
)

echo.
pause
