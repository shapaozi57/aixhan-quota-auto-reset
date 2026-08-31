@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
