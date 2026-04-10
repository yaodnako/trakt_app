@echo off
setlocal
cd /d "%~dp0"

set "WEB_WINDOW_TITLE=Trakt Tracker Web Server"
set "FIREWALL_RULE_NAME=Trakt Tracker Web 8000"

powershell -NoProfile -Command "Get-Process | Where-Object { $_.MainWindowTitle -like '*%WEB_WINDOW_TITLE%*' } | Stop-Process -Force" >nul 2>&1
taskkill /FI "WINDOWTITLE eq *%WEB_WINDOW_TITLE%*" /T /F >nul 2>&1

for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8000 .*LISTENING"') do (
    powershell -NoProfile -Command "Stop-Process -Id %%p -Force" >nul 2>&1
)

netsh advfirewall firewall show rule name="%FIREWALL_RULE_NAME%" >nul 2>&1
if errorlevel 1 (
    netsh advfirewall firewall add rule name="%FIREWALL_RULE_NAME%" dir=in action=allow protocol=TCP localport=8000 >nul
) else (
    netsh advfirewall firewall set rule name="%FIREWALL_RULE_NAME%" new enable=Yes >nul
)

start "%WEB_WINDOW_TITLE%" cmd /k "title %WEB_WINDOW_TITLE% && python -c ^"import uvicorn; uvicorn.run('trakt_tracker.web.app:app', host='0.0.0.0', port=8000, reload=False)^""
echo External access enabled on port 8000.