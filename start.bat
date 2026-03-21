@echo off
REM Telegram Translator - start web app + bot + ngrok
cd /d "%~dp0"

if not exist .env (
    echo .env not found. Create it with BOT_TOKEN, etc. See README.
    pause
    exit /b 1
)

REM Expose app (port 8000) via ngrok in a separate window
start "ngrok" ngrok http 8000

python start_all.py
if errorlevel 1 pause
