@echo off
cd /d "%~dp0"
if not exist .venv (py -3.10 -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 (echo Installation failed. Check internet connection. & pause & exit /b 1)
python app.py
pause
