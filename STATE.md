# Trakt Tracker State

This file is a compact current-state reference. It is not mandatory startup
context; new chats should read `AGENTS.md` first and open this file only when
they need current behavior or policy details.

## Current Facts

- `web` is the primary UI.
- `desktop` is the secondary UI over the same Python core and the same SQLite DB.
- `History` and `Progress` use shared metadata tables:
  - `titles`
  - `episodes_cache`
- SQLite is the source of truth for enrich state, statuses, and refresh timestamps.
- Provider/file caches are response caches, not decision authority.

## Normal Behavior

- `History` and `Progress` use queue-driven patch refresh, not full page reload convergence.
- Visible viewport entries get higher-priority refresh.
- Stale visible refresh is scoped to ratings/details.
- Ready poster/still artwork is not periodically rechecked like ratings.
- Screenshot workflow is required after UI or visible behavior changes:
  - run `capture_web_ui.bat`
  - inspect the generated screenshots
  - iterate if the visible result is wrong

## Metadata Policy

- `title_ratings`:
  - `ready`: 5 minutes on visible stale refresh
  - `checked_no_data`: 6 hours
  - `retryable_failure`: 30 minutes
- `episode_ratings`:
  - `ready`: 5 minutes on visible stale refresh
  - `checked_no_data`: 6 hours
  - `retryable_failure`: 30 minutes
- `poster`:
  - `ready`: not rechecked by normal viewport/sync; only manual repair
  - `checked_no_data`: 7 days
  - `retryable_failure`: 6 hours
- `still`:
  - `ready`: not rechecked by normal viewport/sync; only manual repair
  - `checked_no_data` for recent released + visible (`viewport`): about 5 minutes
  - `checked_no_data` for recent released + non-visible (`page_context` / `sync_event`): about 1 hour
  - `checked_no_data` for old / unknown / unreleased: 7 days
  - `retryable_failure`: 6 hours

## Refresh Triggers

- `viewport`: actually visible cards/groups.
- `page_context`: nearby/page buckets that are loaded but not actually visible.
- `visible_ratings_refresh`: ratings/details stale refresh only.
- `sync_event`: targeted sync-driven refresh.
- `manual_repair`: explicit repair override.

## Current Web Features

- `Progress`
- `History`
- `Search`
- `Settings`
- title/details page
- show-level episode ratings matrix overlay
- web sync modes: `Full Sync`, `Sync`, `Timeout Sync`, `Repair Sync`

## Investigation Checklist

For History/Progress metadata bugs:

1. Check SQLite row values in `titles` / `episodes_cache`.
2. Check enrich statuses and refresh timestamps.
3. Check queue updates and `retryable_failure` state.
4. Check route trigger and `requested_parts`.
5. For UI issues, run and inspect screenshot workflow.

## Recent Fix Notes

- Episode still retry is now release-aware and visibility-aware.
- Fresh `checked_no_data` stills no longer wait 7 days when the episode is already released and visible.
- The confirmed The Boys S05E01/S05E02 stale negative case was resolved through the shared policy path.
- Episode ratings matrix overlay is DB-first and reads from shared `episodes_cache`.

## 2026 Matrix State Notes

- Matrix rating source is selectable with one-click buttons: `IMDb` (default) / `Trakt` / `My ★`.
- First switch to `Trakt` can trigger quick missing Trakt-details fetch (`refresh_missing=1`) for unresolved episode rows.
- Matrix `ALL` average excludes season `0`.
- In `Trakt` mode, unreleased episodes and zero-vote Trakt ratings are treated as unrated (`?`) and are excluded from Trakt averages.
- Matrix tooltip votes follow the active display source.

## 2026 History Card Rating Rendering Notes

- `History` title chips now render `n/a` when `title_ratings_status` is `ready` or `checked_no_data` but a specific provider value is absent.
- `Loading` is now limited to unresolved states (for example `unknown` / retry in progress), to avoid sticky loading labels.
- History filter controls auto-apply without the old `Apply` button.

## Current Limitation: History Ratings Sync Scope

- History rating import currently fetches Trakt ratings for `episode`, `show`, and `movie` types.
- `season` ratings are not currently imported by the history ratings sync path.

## 2026 Sync / Image Runtime Notes

- Watch-history sync fetches both the general history stream and the movie-specific history stream, then dedupes by Trakt history id.
- Search no longer blocks first render on TMDb / IMDb metadata enrichment.
- `/cached-image` returns freshly fetched bytes on cache miss when possible; redirect to the external image remains the fallback path.
