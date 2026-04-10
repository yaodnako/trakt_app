@echo off
setlocal
cd /d "%~dp0"

set "FIREWALL_RULE_NAME=Trakt Tracker Web 8000"

netsh advfirewall firewall show rule name="%FIREWALL_RULE_NAME%" >nul 2>&1
if not errorlevel 1 (
    netsh advfirewall firewall set rule name="%FIREWALL_RULE_NAME%" new enable=No >nul
)

call restart_trakt_tracker_web.bat
echo External access disabled. Local web restarted on 127.0.0.1:8000.
