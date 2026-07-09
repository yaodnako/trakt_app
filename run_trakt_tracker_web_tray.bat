@echo off
setlocal
cd /d "%~dp0"
start "" pythonw -m trakt_tracker.web_tray
