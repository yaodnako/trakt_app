# Trakt Tracker State

This file tracks current temporary reality. Keep it about what is true now, not how the project got here.

## Current Product State

- `web` is the primary day-to-day UI.
- `desktop` still exists and uses the same shared core and SQLite database.
- Main web surfaces are `Progress`, `History`, `Search`, `Settings`, and the title details page.

## Current Metadata Behavior

- `History` and `Progress` read shared metadata from `titles` and `episodes_cache`.
- Web pages use queue-driven patch refresh instead of relying on full-page reload convergence.
- Visible entries are refreshed ahead of non-visible entries.
- Ratings/details may refresh on short stale windows; resolved artwork is treated more conservatively.
- The episode ratings matrix supports `IMDb`, `Trakt`, and `My rating` display modes.
- Episode IMDb ids and ratings are resolved from Trakt ids plus local IMDb dataset episode/title evidence before being persisted, including matrix-open repair for stale cached episode IMDb fields.
- Web image delivery is proxy-first: `/cached-image` serves local cache or freshly fetched bytes and should not rely on the browser following upstream artwork URLs.
- Artwork fetch in `/cached-image` uses the app process first and can fall back to a short `python.exe` helper from the tray `pythonw.exe` runtime to tolerate CDN/proxy differences.

## Current Operations

- Web sync modes currently exposed in settings: `Full Sync`, `Sync`, `Timeout Sync`, `Repair Sync`.
- The web portal can run through the tray launcher (`pythonw -m trakt_tracker.web_tray` or `run_trakt_tracker_web_tray.bat`) without a console window.
- The tray launcher polls upcoming episode notifications itself and can play the configured notification sound even when no browser tab is open.
- Episode notifications wait for the configured post-release delay before firing; the default delay is 120 minutes so translations have time to appear.
- Notification polling can use the current progress next-episode air date when Trakt calendar entries omit `first_aired`; `New` cards require an explicit action button to mark the notification seen.
- Settings can register the tray web portal for Windows user-login autostart.
- Kinopoisk play URLs use a configurable selected domain tail from a comma-separated option list.
- `capture_web_ui.bat` is the standard broad web screenshot pass.
- The tray launcher is the standard day-to-day web runtime; use `run_trakt_tracker_web_tray.bat` or `pythonw -m trakt_tracker.web_tray` for manual verification runs.

## Current Known Limits

- IMDb episode freshness depends on the local official IMDb dataset snapshot; there is no separate incremental feed in the current implementation.
- History ratings import currently covers `episode`, `show`, and `movie` ratings, but not `season` ratings.
- No dedicated lint configuration is present in the repo today.
- The in-app browser can diverge from external browser behavior on interactive image-loading flows; regression sign-off should be done in Edge/Chrome.
