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
- [episode_ratings_matrix.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/episode_ratings_matrix.py)
- [enrich_queue.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/enrich_queue.py)
- [metadata_refresh_policy.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/metadata_refresh_policy.py)
- [routes_history.py](/D:/CodexProjects/Trakt_app/trakt_tracker/web/routes_history.py)
- [routes_progress.py](/D:/CodexProjects/Trakt_app/trakt_tracker/web/routes_progress.py)

## Stable shared model

После фаз 1–5 зафиксирована такая архитектурная модель:

- SQLite — source of truth для enrich state
- `titles` хранит:
  - poster
  - title-level ratings
  - enrich statuses для title metadata
  - refresh timestamps для title-level ratings
  - refresh timestamps для poster artwork
- `episodes_cache` хранит:
  - still
  - episode Trakt details / ratings
  - episode IMDb metadata
  - enrich statuses для episode metadata
  - refresh timestamps для episode ratings/details
  - refresh timestamps для still artwork
- `History` и `Progress` читают одни и те же shared metadata tables
- `episode ratings matrix overlay` для show cards тоже собирается из shared `episodes_cache`
- decision о том, нужен ли enrich, принимается централизованно по:
  - SQLite statuses
  - SQLite refresh timestamps
  - refresh trigger
  - requested refresh parts

## Queue model

Shared queue primitives живут в `application`, а не в web routes:

- queue process-local и in-memory
- queue делает:
  - dedupe по stable task key
  - visible-first priorities
  - ограничение concurrency
  - structured updates для polling
  - merge scoped refresh intents по одному task key
- `History` и `Progress` используют один и тот же queue service
- route-level background start для enrich больше не является основной моделью

## Web refresh model

Для web экранов теперь целевая схема такая:

- SSR page render берет текущее состояние из SQLite
- enrich не должен зависеть от full page reload
- клиент делает patch-only refresh affected cards/groups
- `History` и `Progress` используют JSON refresh endpoints и queue revisions
- entering viewport триггерит быстрый debounce refresh для видимых cards/groups
- visible ratings могут быть stale-refreshed без whole-page reload
- stale visible refresh обновляет только ratings/details
- posters / stills не должны жить на частом periodic refresh path после успешного resolve
- artwork recheck разрешен только на редких event-driven triggers:
  - first-seen missing artwork
  - sync event для новых identity
  - `next_episode` change
  - manual repair

## Web overlay model

- show-level rating chip в `History` / `Progress` может открывать встроенный overlay
- overlay живет на уровне `base.html`, а не внутри конкретного page fragment
- patch replace карточек не должен ломать overlay trigger, потому что trigger binding делается delegated event handling
- overlay matrix route:
  - `GET /titles/show/{trakt_id}/episode-ratings-matrix`
  - возвращает HTML fragment, а не full page
- matrix service использует `DB-first` path:
  - сначала читает `episodes_cache`
  - hydrate через `get_show_episodes()` делает только если данных нет или идет explicit retry
- episode cell может вести на IMDb title page эпизода, если у row есть `imdb_id`

## Repair sync model

- обычные `sync event` / `viewport` refresh paths не обязаны массово разбирать старые artwork rows в `checked_no_data` / `retryable_failure`
- для этого добавлен отдельный web-triggered `Repair Sync`
- `Repair Sync` использует `manual_repair` trigger и проходит по:
  - title posters со статусами `checked_no_data` / `retryable_failure`
  - episode stills со статусами `checked_no_data` / `retryable_failure`
- это отдельный maintenance path, а не замена обычному `Full Sync` / `Sync` / `Timeout Sync`

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
## 2026 Search Notes

- Web `Search` is no longer a raw Trakt-only render path.
- Search SSR merges existing SQLite title metadata before rendering result cards, so cached poster / IMDb / Trakt values can be shown immediately.
- Search poster loading now follows the same cached-image -> direct-image fallback pattern already used by `History` / `Progress`.
- Search show cards reuse the same base-level episode ratings matrix overlay trigger and route as other web screens.

## 2026 Episode Still Retry Architecture Notes

- Shared refresh policy now distinguishes visible and non-visible artwork requests using triggers:
  - `viewport` for visible cards/groups
  - `page_context` for nearby/page buckets
- `ASSET_KIND_STILL` `checked_no_data` decisions now use `first_aired` from shared episode metadata:
  - recent released episodes get short retry windows (5m visible, 1h non-visible)
  - old/unknown/unreleased episodes keep long fallback TTL
- This avoids web-only branching and keeps retry behavior in shared application policy.
