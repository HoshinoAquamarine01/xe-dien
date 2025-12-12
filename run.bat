@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
echo Starting EV Route Optimizer...
python app.py
pause