# Trakt Tracker Architecture

## Boundaries

- `trakt_tracker/web`: web routes, templates, browser-facing refresh endpoints.
- `trakt_tracker/web_tray.py`: PySide tray launcher for running the web portal as a background child process with a log window.
- `trakt_tracker/ui`: desktop UI.
- `trakt_tracker/application`: use cases, sync workflows, enrich policies, queue orchestration, read models.
- `trakt_tracker/persistence`: SQLite models and repositories.
- `trakt_tracker/infrastructure`: provider clients, caches, notifications, keyring, transport details.

## Source Of Truth

- SQLite is the source of truth for enrich state, cached metadata, statuses, and refresh timestamps.
- `titles` stores title metadata such as poster, title-level ratings, and title refresh timestamps.
- `episodes_cache` stores episode metadata such as stills, Trakt details, IMDb episode ratings, and episode refresh timestamps.
- Provider response caches are helpers only. They do not decide what the UI should show.

## Data Flow

1. Provider clients fetch remote data from Trakt, TMDb, IMDb datasets, and other configured providers.
2. Application services normalize and merge that data. Episode IMDb metadata passes through an application resolver before IMDb ids and ratings are persisted, so provider id mismatches do not attach ratings to the wrong episode.
3. Persistence repositories write normalized state into SQLite.
4. Web and desktop read from the same SQLite-backed model.
5. Background refresh work is routed through shared application services rather than UI-specific logic.

## Refresh Model

- `History` and `Progress` use shared enrich logic and the same queue service.
- Refresh tasks are scoped by trigger and requested parts instead of using a coarse global force-refresh path.
- The web UI renders current SQLite state first, then applies patch-style refreshes for affected cards or groups.
- Show-level episode ratings matrix reads from shared episode metadata, not from a separate web-only cache.

## Invariants

- Shared behavior should live outside UI layers.
- Web-only routes may orchestrate requests, but they should not become the decision authority for metadata state.
- A resolved artwork row should not be put on the same frequent polling path as ratings.
- Episode IMDb ratings must be persisted only after resolving the episode identity from available Trakt and IMDb dataset evidence.
- If UI and stored state disagree, inspect SQLite and enrich status first.
