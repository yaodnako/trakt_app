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
- next-episode still / Trakt / IMDb metadata
- queue-driven in-place refresh без whole-page reload
- stable loading / empty states

## History

Поддерживается:

- watch rows
- grouped-by-day cards в web
- title posters в web history cards
- title-level Trakt / IMDb chips
- episode stills
- episode rating display
- Trakt episode aggregate rating / votes
- IMDb rating / votes
- filters
- sync
- rate item
- queue-driven in-place refresh без whole-page reload
- stable loading / empty states

## Shared core behavior

`History` и `Progress` теперь опираются на один и тот же shared metadata/enrich слой:

- explicit enrich statuses в SQLite
- shared title / episode metadata tables
- visible-first enrich queue
- patch refresh вместо page reload

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
