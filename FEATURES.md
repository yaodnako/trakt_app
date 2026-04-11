# Trakt Tracker Features

## Desktop

Работает:

- Trakt auth
- search
- search sort
- saved search state
- history
- ratings
- progress
- upcoming
- notifications
- settings
- play flow

## Web

Работает:

- `Progress`
- `History`
- `Search`
- `Settings`
- title/details page
- show-level episode ratings matrix overlay

## Progress

Поддерживается в desktop и web:

- `Sync`
- `Hide Upcoming`
- `Show Dropped`
- year filter
- `Play`
- `Watched`
- `Seen`
- `Drop` / `Undrop`
- post-watch rating flow
- shared title poster / title-level ratings
- title poster click -> Trakt title page
- title rating chip click -> episode ratings matrix overlay
- next-episode preview click -> Trakt episode page
- next-episode still / Trakt / IMDb metadata
- queue-driven in-place refresh без whole-page reload
- viewport-driven refresh для видимых cards
- stale title / episode ratings refresh без full page reload
- artwork не уходит в частый polling после успешного resolve
- next-episode still recheck идет только по targeted policy, а не при каждом ratings refresh
- skipped-count badge на title poster
- average rated-episodes badge на title poster
- stable loading / empty states

## History

Поддерживается:

- watch rows
- grouped-by-day cards в web
- title posters в web history cards
- title poster click -> Trakt title page
- title-level Trakt / IMDb chips
- title rating chip click -> episode ratings matrix overlay for shows
- episode stills
- episode still click -> Trakt episode page
- episode rating display
- Trakt episode aggregate rating / votes
- IMDb rating / votes
- filters
- sync
- rate item
- queue-driven in-place refresh без whole-page reload
- viewport-driven refresh для видимых groups
- stale title / episode ratings refresh без full page reload
- episode still artwork не должен периодически refresh-иться как ratings
- stable loading / empty states

## Episode Ratings Matrix Overlay

Поддерживается в web:

- show-only overlay на текущей странице
- compact season/episode matrix по IMDb ratings
- сезонные averages (`AVG.`)
- `Hide season 0`
- tooltip на ячейке:
  - название эпизода
  - `Sxx Exx`
  - votes
- click по ячейке -> IMDb episode page, если в SQLite есть episode `imdb_id`
- `DB-first` open path:
  - SQLite rows -> instant open
  - forced network hydrate только по retry-path

## Shared core behavior

`History` и `Progress` теперь опираются на один и тот же shared metadata/enrich слой:

- explicit enrich statuses в SQLite
- refresh timestamps в SQLite для ratings/details/artwork
- shared title / episode metadata tables
- visible-first enrich queue
- patch refresh вместо page reload
- viewport-triggered refresh для visible items
- ratings refresh policy отдельно от artwork policy
- event-driven artwork policy:
  - `sync event`
  - `next_episode` change
  - first-seen missing artwork
  - manual repair
- scoped refresh intent вместо coarse `force_refresh`

## Debug

Есть `Debug mode`:

- desktop
- web

Он уже показывает operation/debug events, но это вспомогательный режим, а не основной UX-механизм экранов.

## Settings

Поддерживается:

- cache TTL
- notifications polling interval
- IMDb auto-sync interval
- manual web sync actions:
  - `Full Sync`
  - `Sync`
  - `Timeout Sync`
  - `Repair Sync`
## Search

Web search supports:

- compact vertical result cards
- show-card title rating chip -> episode ratings matrix overlay
- SQLite-backed cached poster / IMDb / Trakt metadata on SSR
- `IMDb votes` sorting based on enriched IMDb values
- direct-image poster fallback when cached-image proxy fails

## Episode Ratings Matrix Overlay

Additional current UI details:

- `AVG.` is rendered as the first visible row of the matrix
- `ALL` applies only to the average line, not to episode rows

## 2026 Episode Still Retry Behavior

- `History` and `Progress` now share release-aware retry for episode stills when status is `checked_no_data`.
- Visible placeholders retry frequently (about every 5 minutes), while non-visible page-context retries are slower (about every 1 hour) for recent released episodes.
- Old / unknown / unreleased episode still negatives remain on long retry intervals to avoid unnecessary TMDb polling.

## 2026 Matrix Provider Behavior

- Episode ratings matrix now uses one-click icon buttons for `IMDb`, `Trakt`, and `My ★`.
- First switch to `Trakt` rechecks due episode Trakt ratings through shared policy:
  - released within 10 days: matrix foreground TTL 5 minutes
  - older rows: background-only refresh path
- Matrix `ALL` average excludes season `0`.
- In `Trakt` mode, unreleased episodes and zero-vote Trakt ratings are shown as unrated (`?`) and excluded from Trakt averages.
- Matrix tooltips now follow the currently displayed source values.
- Web background loop periodically enqueues older due episode Trakt-rating refreshes:
  - 10-60 days: 6 hours
  - 60-180 days: 48 hours
  - 180-720 days: 14 days
  - 720+ days / unknown air date: 60 days

## 2026 History Title Card Rating Fallback

- For title-level chips in `History` cards (`episodes` and `titles` views):
  - if rating value exists: show rating
  - if status is `checked_no_data` or `ready` but value is missing: show `n/a`
  - `Loading` is reserved for unresolved states only
- History filters apply without an `Apply` button:
  - type / rated-only changes submit immediately
  - title text filter submits after a short debounce

## 2026 Sync And Image Loading Notes

- History sync reads both the general watch-history stream and the movie watch-history stream, then dedupes by Trakt history id.
- Search page metadata enrichment now runs in the background instead of blocking initial render.
- Cached web images now try to fetch and return bytes on first cache miss before falling back to external redirect.
