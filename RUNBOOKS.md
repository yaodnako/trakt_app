# Trakt Tracker Runbooks

Repeatable workflows only. Keep product state in `STATE.md` and durable decisions in `DECISIONS.md`.

## Web UI Verification

1. Run the web UI through the tray runtime when verifying normal user behavior: `run_trakt_tracker_web_tray.bat` or `pythonw -m trakt_tracker.web_tray`.
2. Open `http://127.0.0.1:8000` in Edge or Chrome.
3. Use targeted HTML or screenshots for the affected surface.
4. Use `capture_web_ui.bat` when broad layout coverage is needed.

The in-app browser is supplemental; sign off interactive web UI changes in Edge or Chrome.

## Search UI Regression Check

For Search UI changes, verify these together:

- poster image loads
- IMDb does not stay `Loading`
- provider rating chips render
- top-right personal rating badge appears where user ratings exist
- outside play button exists
- outside mark-watched button exists for movies and shows
- show poster click opens the episode watch panel

## Metadata Bug Triage

1. Check SQLite row values in `titles` or `episodes_cache`.
2. Check enrich status and refresh timestamps.
3. Check whether queue work is stuck in `retryable_failure`.
4. Check whether the route asked for the right trigger and requested parts.

## Episode IMDb Rating Repair

Use when episode IMDb ratings are missing or appear shifted after provider metadata changed.

1. Confirm the local IMDb dataset is ready and recent in `%LOCALAPPDATA%\TraktTracker\imdb\ratings.sqlite3`.
2. Check `episodes_cache.imdb_id`, `imdb_rating`, and `imdb_votes` for affected episodes.
3. Run `EpisodeMetadataService.repair_episode_imdb_ratings(show_trakt_id=...)` for a narrow repair, or without `show_trakt_id` for a full cached-episode repair.
4. Restart the tray web runtime if the UI was already open: stop existing `trakt_tracker.web_tray` / `trakt_tracker.web.main`, then start `pythonw -m trakt_tracker.web_tray`.
5. Verify the matrix endpoint, for example `/titles/show/{trakt_id}/episode-ratings-matrix?provider=imdb`, before relying on browser state.
