# Trakt Tracker

Windows-first Trakt tracker with two UIs over one shared Python core:

- `web`: `FastAPI + Jinja2`
- `desktop`: `PySide6`

## Requirements

- Python 3.11+
- Windows PowerShell / batch files for the helper scripts in this repo

## Install

Desktop-only dependencies:

```powershell
python -m pip install -e .
```

Web UI dependencies:

```powershell
python -m pip install -e ".[web]"
```

Dev dependencies:

```powershell
python -m pip install -e ".[web,dev]"
```

## Run

Desktop:

```powershell
python -m trakt_tracker.main
```

Web:

```powershell
python -m trakt_tracker.web.main
```

Web tray launcher:

```powershell
pythonw -m trakt_tracker.web_tray
```

Helper scripts:

- `run_trakt_tracker.bat`
- `run_trakt_tracker_web.bat`
- `run_trakt_tracker_web_tray.bat`
- `restart_trakt_tracker_web.bat`

The web app listens on `http://127.0.0.1:8000`.

## Tests

```powershell
pytest
```

## Lint

No dedicated linter is configured in the repo today.

## Build

Package build smoke check:

```powershell
python -m pip install build
python -m build
```

## Basic Verification

1. Start the web app.
2. Open `http://127.0.0.1:8000` in an external browser (`Edge`/`Chrome`).
3. Confirm `Progress`, `History`, `Search`, and `Settings` load.

Use `RUNBOOKS.md` for targeted interactive and regression checks.

Broad web visual check:

```powershell
capture_web_ui.bat
```

Artifacts are written to `generated/ui_checks`.
