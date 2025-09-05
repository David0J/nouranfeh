@echo off
setlocal EnableExtensions
title Nour Anfeh – App

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo venv not found. Run "setup_windows.bat" first.
  pause
  exit /b 1
)

call "venv\Scripts\activate"
python "nour_anfeh_gui.py"