# Trakt Tracker Architecture

## Базовый принцип

- `desktop` и `web` это два UI-слоя над одним core
- не дублировать business logic между UI
- UI не должен напрямую тащить provider logic

## Слои

- `trakt_tracker/ui`
  Desktop UI only
- `trakt_tracker/web`
  Web UI only
- `trakt_tracker/application`
  Use-cases, orchestration, sync policies, read models
- `trakt_tracker/infrastructure`
  Trakt / TMDb / Kinopoisk / notifications / cache / keyring
- `trakt_tracker/persistence`
  SQLite models and repositories

## Что уже разрезано

Сейчас core уже не полностью монолитный.

Отдельно вынесены:

- [sync_policy.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/sync_policy.py)
- [operations.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/operations.py)
- [history_sync.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history_sync.py)
- [progress_sync.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/progress_sync.py)
- [notification_refresh.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/notification_refresh.py)
- [catalog.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/catalog.py)
- [interactions.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/interactions.py)
- [history_read_model.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history_read_model.py)
- [episode_metadata.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/episode_metadata.py)
- [trakt_payload_cache.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/trakt_payload_cache.py)

## Что ещё остаётся тяжёлым

- [services.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/services.py)
  Уже стал меньше, но ещё держит history/rating write-side orchestration
- [main_window.py](/D:/CodexProjects/Trakt_app/trakt_tracker/ui/main_window.py)
  Всё ещё слишком большой orchestration class
- [app.py](/D:/CodexProjects/Trakt_app/trakt_tracker/web/app.py)
  Всё ещё толстый web shell

## Правила дальнейшей работы

- сначала ревизия, потом следующий рефактор
- не говорить, что этап “внедрён”, пока нет реального diff
- для code changes использовать patch-only workflow
- после каждого этапа:
  - убедиться, что `git diff` не пустой
  - только потом считать этап реальным

## Порядок дальнейшего рефактора

1. Разрезать UI orchestration
   Сначала desktop, потом web

2. Упростить `Debug mode`
   Один поток operation events, меньше локальных UI-hints

3. Только потом делать performance pass
   Не оптимизировать поверх ещё не до конца очищенной структуры

## Важная политика sync

- `History`
  auto-sync по `last_activities` + probe interval
- `Progress`
  full sync реже, focused/visible refresh чаще
- `Notifications`
  опираются на текущий `next episode`, не на произвольный calendar item
- `Episode metadata`
  должны жить через единый metadata layer, не размазываться по разным сервисам
