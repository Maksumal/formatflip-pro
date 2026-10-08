@echo off
setlocal
cd /d "%~dp0"
echo === FormatFlip Pro ===
set "PYTHON=python"
where python >nul 2>nul || set "PYTHON=py -3"
if not exist ".venv\Scripts\python.exe" (
  echo Creating private Python environment...
  %PYTHON% -m venv .venv
  if errorlevel 1 goto :error
)
echo Checking dependencies...
.venv\Scripts\python.exe -m pip install -r server\requirements.txt
if errorlevel 1 goto :error
.venv\Scripts\python.exe -c "import shutil,sys,os; p=shutil.which('ffmpeg') or r'C:\Users\chann\AppData\Local\hermes\tools\ffmpeg-9.0.1-win32-x64\bin\ffmpeg.exe'; sys.exit(0 if os.path.isfile(p) else 1)"
if errorlevel 1 (
  echo FFmpeg was not found. Install FFmpeg or see README.md.
  pause
  exit /b 1
)
echo.
echo Starting FormatFlip at http://127.0.0.1:5000
echo Keep this window open while you use the site. Press Ctrl+C to stop.
echo.
.venv\Scripts\python.exe -m waitress --listen=127.0.0.1:5000 --threads=4 server.app:app
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo Could not start. Check Python and internet access, then run start.bat again.
pause
exit /b 1
