# Trakt Tracker Agent Bootstrap

Read this file first in new chats. Do not load all project docs by default.
Use the larger docs only when the task needs that specific detail:

- `README.md`: run commands, web restart, screenshot workflow.
- `ARCHITECTURE.md`: shared core boundaries and refresh architecture.
- `STATE.md`: current policy/state when investigating behavior.
- `FEATURES.md`: product/UI surface inventory.

## Project Shape

- `web` (`FastAPI + Jinja2`) is the primary UI.
- `desktop` (`PySide6`) is the secondary UI over the same Python core and the same SQLite database.
- Shared behavior belongs in `trakt_tracker/application` or `trakt_tracker/persistence`, not in web-only routes/templates.
- SQLite is the source of truth for enrich state:
  - `titles` stores title metadata, posters, title ratings, and refresh timestamps.
  - `episodes_cache` stores episode metadata, stills, episode ratings/details, and refresh timestamps.
- Provider response caches are not decision authority.

## Working Rules

- Patch-only for manual edits.
- Do not revert unrelated dirty worktree changes.
- If Python web code changes and verification needs it, restart the web server yourself with `restart_trakt_tracker_web.bat`.
- After UI or visible behavior changes, run screenshot workflow with `capture_web_ui.bat`, inspect the screenshots, and iterate if the result is wrong.
- Keep responses concise.

## Refresh And Queue Model

- `History` and `Progress` use the shared enrich queue and patch-only JSON refresh paths.
- Queue tasks carry `trigger + requested_parts`; avoid coarse `force_refresh` semantics for normal visible refresh.
- Main triggers:
  - `viewport`: actually visible cards/groups.
  - `page_context`: non-visible page/nearby buckets.
  - `visible_ratings_refresh`: stale visible ratings/details only.
  - `sync_event`: targeted sync-driven refresh.
  - `manual_repair`: explicit repair override.
- Ratings/details can use short stale refresh.
- Ready artwork should not be periodically rechecked like ratings.

## Episode Still Policy

- Episode still retry is release-aware and visibility-aware in shared policy.
- For `still_status=checked_no_data` with empty `still_url`:
  - recent released + visible (`viewport`): retry about every 5 minutes.
  - recent released + non-visible (`page_context` / `sync_event`): retry about every 1 hour.
  - old / unknown / unreleased: keep long fallback TTL, currently 7 days.
- This exists to avoid stale negative stills such as fresh episodes whose TMDb still appeared after the first check.

## Investigation Checklist

When History/Progress artwork or ratings look wrong, check in this order:

1. SQLite row values in `titles` / `episodes_cache`.
2. Enrich statuses and refresh timestamps.
3. Queue updates and whether the task is stuck in `retryable_failure`.
4. Whether the route requested the right trigger and requested parts.
