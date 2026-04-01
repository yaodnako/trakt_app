# Trakt Tracker State

This file records current technical state, important decisions, and known limitations.

## Product Direction

- Single-user local Windows desktop app
- Trakt remains the source of truth for:
  - watch history
  - Trakt ratings
  - progress
  - upcoming calendar
- TMDb is enrichment for artwork and metadata
- Official IMDb datasets are the source for IMDb rating/vote data

## Current Technical Shape

- UI: `PySide6`
- Web prototype: `FastAPI + Jinja2` over the existing Python core
- HTTP: `httpx`
- Storage: `SQLite` via SQLAlchemy
- Token storage: `keyring`
- Search UI: `QListView + custom model + delegate`
- History UI: `QTableWidget`

## Important Decisions

- `Dashboard` was removed; `History` is the default first tab
- Current desktop UI should be kept as the frozen baseline while evaluating a web prototype
  - do not throw away working desktop flows before the web proof of concept proves itself
- Web prototype should stay a thin UI shell
  - reuse existing services/repositories first
  - do not fork business logic into a second stack
- Web `Progress` now reuses the same progress/drop/watch/rating core as desktop
  - web-specific logic stays in routing/template/viewmodel only
- Search is local-first for repeated queries when saved results already exist
- History tab uses incremental sync for watch history, but ratings are still fully re-synced
- Long result lists use model/delegate rendering instead of `setItemWidget`
- Episode rating in `History` is only episode rating
  - show-level rating must not bleed into episode rows
- IMDb data should be taken from official IMDb datasets, not OMDb
- If a fix request conflicts with best practice, explain and choose the better boundary
- Broad user requests should be interpreted at the product level before implementation
  - avoid literal UI implementations when the app already has local data that can drive a better interaction
- Performance issues should be handled measurement-first
  - startup/lag work must begin with instrumentation or existing timings, not blind optimization

## Known Data Limitations

- Some episode IMDb ratings are genuinely missing from official IMDb `title.ratings.tsv.gz`
- Some episode IMDb IDs are missing in Trakt payloads
- Some series have season numbering mismatches between Trakt and IMDb
  - this is why episode-title fallback exists in the IMDb pipeline
- A title/episode may be identifiable in IMDb datasets but still have no rating row

## Confirmed Example Of Upstream Limitation

- `Hell's Paradise` / `Ephemeralness and Fire`
  - Trakt episode payload: `episode.ids.imdb = null`
  - TMDb episode external IDs: `imdb_id = null`
  - official IMDb episode map can identify the episode by title
  - official IMDb ratings dataset currently has no rating row for that episode
  - result: empty IMDb cell is currently correct

## Known Code/UX Constraints

- Search result enrichment can change ordering after data arrives
- Posters are cached separately from search text results
- Poster loading still depends on image cache/network availability for first fetch
- Existing local databases may require dataset/schema refresh after IMDb index structure changes

## Cleanup Already Done

- Removed `Dashboard`
- Removed OMDb from active IMDb pipeline
- Removed unnecessary TMDb episode-ID fallback from the history IMDb path
- Fixed poster disk-cache path mismatch
- Fixed repeated loading attempts for failed poster URLs in the same session
- Added startup timing log:
  - `C:\Users\yaodn\AppData\Local\TraktTracker\startup.log`
  - used to measure launch stages before further startup optimization

## Process Rules For This Project

- Validate targeted data bugs against the exact affected rows before saying a fix is done
- Use minimal sufficient checks, not broad noisy validation
- Distinguish missing upstream data from local application bugs
- Relaunch GUI after targeted self-diagnosis, not instead of it
- For performance work, capture timings first and optimize second
- Do not swap `Progress`/sync endpoints based on naming alone
  - inspect payload and coverage first
  - `sync/progress/up_next` was tested in the main path once and did not match the full backlog-style `Progress` screen
- Web prototype now writes:
  - `C:\Users\yaodn\AppData\Local\TraktTracker\web_startup.log`
  - `C:\Users\yaodn\AppData\Local\TraktTracker\web_request_timings.log`

## If Migrating To Web Later

- Immediate migration direction:
  - keep Python services/repositories as the core
  - treat the current PySide UI as a preserved baseline
  - build a small web prototype first instead of a full rewrite
- Preserve these feature groups first:
  - Trakt auth
  - search
  - local-first saved search state
  - ratings/history
  - episode IMDb enrichment
  - upcoming notifications logic
- First web prototype scope should stay small:
  - `Progress`
  - `History`
  - `Search`
  - title/details page
  - optional simple watch page with external URL/iframe shell later
- Reuse these domain concepts:
  - `TitleSummary`
  - `EpisodeSummary`
  - history event normalization
  - provider boundary: Trakt vs TMDb vs IMDb datasets
