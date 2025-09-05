@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Nour Anfeh – First-time Setup

cd /d "%~dp0"

echo:
echo === Creating Python venv ===
if not exist venv (
  python -m venv venv
) else (
  echo venv already exists.
)

echo:
echo === Activating venv and installing Python packages ===
call "venv\Scripts\activate"
pip install --upgrade pip
if exist requirements.txt (
  pip install -r requirements.txt
) else (
  echo requirements.txt not found. Installing default set...
  pip install PySide6 pandas requests "qrcode[pil]" "urllib3<2"
)

echo:
echo === Installing Node packages for wa_local_api ===
if exist "wa_local_api" (
  cd wa_local_api
  if exist package-lock.json (
    call npm ci
  ) else (
    call npm install
  )
  cd ..
) else (
  echo ERROR: wa_local_api folder not found. Make sure it exists next to this file.
  pause
  exit /b 1
)

echo:
echo Setup complete. You can now use "Run Nour Anfeh (no console).vbs" to launch the app.
pause