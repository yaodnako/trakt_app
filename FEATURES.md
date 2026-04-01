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
- `Drop` / `Undrop`
- `New`
- `Seen`
- post-watch rating flow

## History

Поддерживается:

- watch rows
- episode rating display
- IMDb rating/votes
- filters
- sync
- rate missing item

## Debug

Есть `Debug mode`:

- desktop
- web

Он уже показывает часть operation/debug событий, но ещё не доведён до финального единого потока.

## Что ещё требует доработки

- более чистый и единый debug UX
- further simplification of sync/update paths
- further simplification of desktop/web orchestration layers

## Settings

Поддерживается:

- cache TTL
- notifications polling interval
- IMDb auto-sync interval
