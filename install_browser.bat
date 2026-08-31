@echo off
setlocal
cd /d "%~dp0"
python -m playwright install chromium
pause
