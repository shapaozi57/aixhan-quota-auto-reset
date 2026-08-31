@echo off
setlocal
cd /d "%~dp0"
start "" wscript.exe "%~dp0start_hidden.vbs"
exit /b 0
