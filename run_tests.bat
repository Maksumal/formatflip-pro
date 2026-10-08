@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat first so dependencies can be installed.
  pause
  exit /b 1
)
.venv\Scripts\python.exe server\test_app.py
pause
