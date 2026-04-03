# Trakt Tracker Architecture

## Базовый принцип

- `web` и `desktop` — это два UI-слоя над одним core
- business logic не должна дублироваться между UI
- UI не должен напрямую тянуть provider logic

## Слои

- `trakt_tracker/web`
  Основной web UI
- `trakt_tracker/ui`
  Второй desktop UI
- `trakt_tracker/application`
  Use-cases, orchestration, queue, sync policies, read models
- `trakt_tracker/infrastructure`
  Trakt / TMDb / IMDb / notifications / caches / keyring
- `trakt_tracker/persistence`
  SQLite models и repositories

## Текущее ядро

Core уже не монолитный. Ключевые куски вынесены отдельно:

- [sync_policy.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/sync_policy.py)
- [operations.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/operations.py)
- [history_sync.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history_sync.py)
- [progress_sync.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/progress_sync.py)
- [notification_refresh.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/notification_refresh.py)
- [catalog.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/catalog.py)
- [history.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history.py)
- [history_read_model.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history_read_model.py)
- [episode_metadata.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/episode_metadata.py)
- [enrich_queue.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/enrich_queue.py)
- [routes_history.py](/D:/CodexProjects/Trakt_app/trakt_tracker/web/routes_history.py)
- [routes_progress.py](/D:/CodexProjects/Trakt_app/trakt_tracker/web/routes_progress.py)

## Stable shared model

После фаз 1–5 зафиксирована такая архитектурная модель:

- SQLite — source of truth для enrich state
- `titles` хранит:
  - poster
  - title-level ratings
  - enrich statuses для title metadata
- `episodes_cache` хранит:
  - still
  - episode Trakt details / ratings
  - episode IMDb metadata
  - enrich statuses для episode metadata
- `History` и `Progress` читают одни и те же shared metadata tables
- decision о том, нужен ли enrich, принимается только по SQLite statuses

## Queue model

Shared queue primitives живут в `application`, а не в web routes:

- queue process-local и in-memory
- queue делает:
  - dedupe по stable task key
  - visible-first priorities
  - ограничение concurrency
  - structured updates для polling
- `History` и `Progress` используют один и тот же queue service
- route-level background start для enrich больше не является основной моделью

## Web refresh model

Для web экранов теперь целевая схема такая:

- SSR page render берет текущее состояние из SQLite
- enrich не должен зависеть от full page reload
- клиент делает patch-only refresh affected cards/groups
- `History` и `Progress` используют JSON refresh endpoints и queue revisions

## Что еще остается тяжелым

- [main_window.py](/D:/CodexProjects/Trakt_app/trakt_tracker/ui/main_window.py)
  Desktop orchestration все еще слишком большая
- [services.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/services.py)
  Это уже thin composition root, но он остается важной точкой сборки зависимостей

## Правила дальнейшей работы

- не считать этап “внедренным”, пока нет реального diff и локальной проверки
- для code changes использовать patch-only workflow
- после UI-правок обязательно делать screenshot check
- если на экране виден странный результат, сначала проверять:
  - SQLite row values
  - enrich statuses
  - queue updates
- если баг касается `History` или `Progress`, сначала отделять:
  - данные потерялись
  - данные есть, но экран их неверно показывает
  - данные есть, но queue/retry/status застрял
