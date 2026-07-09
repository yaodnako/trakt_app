# Trakt Tracker Agent Bootstrap

Read this file first in new chats. Open other docs only when the task needs them.

## Doc Map

- `README.md`: install, run, test, package, basic verification.
- `ARCHITECTURE.md`: boundaries, data flow, source of truth, invariants.
- `STATE.md`: current temporary reality, active behavior, known current limits.
- `DECISIONS.md`: non-obvious architectural decisions and why they exist.
- `FEATURES.md`: compact product surface inventory.
- `RUNBOOKS.md`: repeatable verification and triage workflows.

## Project Shape

- `web` (`FastAPI + Jinja2`) is the primary UI.
- `desktop` (`PySide6`) is a secondary UI over the same Python core and the same SQLite database.
- Shared behavior belongs in `trakt_tracker/application` or `trakt_tracker/persistence`, not in web-only routes/templates.

## Working Rules

- Use patch-only edits for manual file changes.
- Do not revert unrelated dirty worktree changes.
- If Python web code changes and verification needs a local server, use the tray web runtime (`run_trakt_tracker_web_tray.bat` or `pythonw -m trakt_tracker.web_tray`) unless the task explicitly targets the legacy windowed server.
- Use `RUNBOOKS.md` for repeatable verification details instead of expanding this bootstrap.
