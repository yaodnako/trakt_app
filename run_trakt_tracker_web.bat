@echo off
setlocal
cd /d "%~dp0"
set "WEB_WINDOW_TITLE=Trakt Tracker Web Server"
powershell -NoProfile -Command "Get-Process | Where-Object { $_.MainWindowTitle -like '*%WEB_WINDOW_TITLE%*' } | Stop-Process -Force" >nul 2>&1
taskkill /FI "WINDOWTITLE eq *%WEB_WINDOW_TITLE%*" /T /F >nul 2>&1
start "%WEB_WINDOW_TITLE%" cmd /k "title %WEB_WINDOW_TITLE% && python -m trakt_tracker.web.main"
powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'" >nul 2>&1
