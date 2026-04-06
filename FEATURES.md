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
- next-episode preview click -> Trakt episode page
- next-episode still / Trakt / IMDb metadata
- queue-driven in-place refresh без whole-page reload
- viewport-driven refresh для видимых cards
- stale title / episode ratings refresh без full page reload
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
- stable loading / empty states

## Shared core behavior

`History` и `Progress` теперь опираются на один и тот же shared metadata/enrich слой:

- explicit enrich statuses в SQLite
- refresh timestamps в SQLite для ratings/details
- shared title / episode metadata tables
- visible-first enrich queue
- patch refresh вместо page reload
- viewport-triggered refresh для visible items
- ratings refresh policy отдельно от artwork policy

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
