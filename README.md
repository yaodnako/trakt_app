# Trakt Tracker

Windows-first Trakt tracker with a `FastAPI + Jinja2` web portal and a PySide system-tray launcher.

## Requirements

- Python 3.11+
- Windows PowerShell / batch files for the helper scripts in this repo

## Install

```powershell
python -m pip install -e .
```

Dev dependencies:

```powershell
python -m pip install -e ".[dev]"
```

## Run

Web:

```powershell
python -m trakt_tracker.web.main
```

The normal web runtime listens only on `127.0.0.1:8000`. The tray prefers port `8000` and selects another loopback port when it is already occupied.

Web tray launcher:

```powershell
pythonw -m trakt_tracker.web_tray
```

The first interactive launch opens the portal after the embedded server is ready. Later launches open the existing instance instead of starting another server. Use `--autostart` to start without opening a browser and `--quit` to stop the running instance.

Helper scripts:

- `run_trakt_tracker_web.bat`
- `run_trakt_tracker_web_tray.bat`
- `restart_trakt_tracker_web.bat`

## Verify

```powershell
python -m pip install -e ".[dev]"
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify.ps1
```

The command runs `pytest`, Ruff and Pyright. To run only static checks, pass `-SkipTests`.

## Build

Install the reproducible Windows release dependency:

```powershell
python -m pip install -e ".[dev,release]"
```

A final build requires `TRAKT_TRACKER_TRAKT_CLIENT_ID`, `TRAKT_TRACKER_TRAKT_CLIENT_SECRET`, and either `TRAKT_TRACKER_TMDB_API_KEY` or `TRAKT_TRACKER_TMDB_READ_ACCESS_TOKEN` in the build process environment. Values are embedded as application defaults, are not written to source files or build logs, and remain technically extractable from a desktop bundle.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_portable.ps1
```

For a package/runtime smoke build without provider defaults:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_portable.ps1 -AllowMissingDefaults
powershell -NoProfile -ExecutionPolicy Bypass -File tools\smoke_portable.ps1
```

The output is `dist\TraktTracker-0.2.0b1-win64-portable.zip` plus its SHA-256 file. It is an unsigned Windows x64 beta: extract the entire folder and run `TraktTracker.exe`. Profiles and other user data remain under `%LOCALAPPDATA%\TraktTracker` when the portable folder is replaced.

## Basic Verification

1. Start the tray app and use `Open portal` so a fallback port is handled automatically.
2. Confirm `/healthz` reports the expected version.
3. Confirm every main surface loads: `Progress`, `History`, `Search`, `Explore`, `Watchlist`, `Releases`, and `Settings`.

Use `RUNBOOKS.md` for targeted interactive and regression checks.
