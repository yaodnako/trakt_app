@echo off
setlocal
cd /d "%~dp0"
for /f %%i in ('powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()"') do set TRAKT_LAUNCH_EPOCH_MS=%%i
start "" cmd /c "set TRAKT_LAUNCH_EPOCH_MS=%TRAKT_LAUNCH_EPOCH_MS% && python -m trakt_tracker.main"
