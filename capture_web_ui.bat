@echo off
setlocal
cd /d "%~dp0"
tools\visual_checks\.venv\Scripts\python tools\capture_web_screens.py --browser chrome --scale 1.25 --zoom 1.25 --pages progress history search settings %*
