# Trakt Tracker

Короткая входная точка для нового чата и новой сессии по проекту.

## Что это сейчас

- `web` на `FastAPI + Jinja2` — основной рабочий UI
- `desktop` на `PySide6` — второй UI поверх того же Python core и той же SQLite
- source of truth:
  - `Trakt` для history / ratings / progress / calendar
  - `TMDb` для artwork и части metadata
  - official `IMDb datasets` для IMDb ratings / votes

## Что читать в новом чате

1. `AGENTS.md` - short mandatory bootstrap for agents.
2. `README.md` - run / restart / screenshot commands when needed.
3. `ARCHITECTURE.md` - read only for architecture or shared-core questions.
4. `STATE.md` - read only for current behavior, policy, or fix history.
5. `FEATURES.md` - read only for product/UI scope.

Do not load all docs by default; choose the detailed file only when the task needs it.

## Запуск

Desktop:

```powershell
python -m pip install -e .
python -m trakt_tracker.main
```

Web:

```powershell
python -m pip install -e ".[web]"
python -m trakt_tracker.web.main
```

Батники:

- [run_trakt_tracker_web.bat](/D:/CodexProjects/Trakt_app/run_trakt_tracker_web.bat)
- [restart_trakt_tracker_web.bat](/D:/CodexProjects/Trakt_app/restart_trakt_tracker_web.bat)

Visual checks:

- [capture_web_ui.bat](/D:/CodexProjects/Trakt_app/capture_web_ui.bat)
- [tools/capture_web_screens.py](/D:/CodexProjects/Trakt_app/tools/capture_web_screens.py)
- screenshots идут в [generated/ui_checks](/D:/CodexProjects/Trakt_app/generated/ui_checks)

## Что уже стабилизировано

Большой stabilization/refactor цикл для `History + Progress` на core/data-flow уровне уже проведен:

- Phase 1:
  - SQLite стала источником истины для enrich state
  - title / episode metadata больше не живут только в file cache
  - sync не должен сносить уже найденные poster / still / ratings
- Phase 2:
  - `History` ушла от reload-driven convergence
  - page-specific refresh идет через JSON patch path, а не через `window.location.reload()`
- Phase 3:
  - введена visible-first enrich queue
  - queue делает dedupe, priority и ограничение concurrency
- Phase 4:
  - `History` получила стабильные loading / empty semantics без misleading `n/a`
- Phase 5:
  - `Progress` переведен на тот же shared enrich core и queue path
  - normal page render больше не должен opportunistically тянуть network enrich
- Post-phase hardening:
  - visible ratings в `History` и `Progress` получили stale-by-time refresh policy
  - entering viewport триггерит быстрый patch refresh для видимых cards/groups
  - posters / stills больше не должны периодически перепроверяться как ratings
  - refresh policy для ratings / poster / still централизована в shared core, а не размазана по `History` / `Progress`
  - queue tasks теперь несут `trigger + requested_parts`, а не грубый `force_refresh`
  - для artwork добавлены SQLite timestamps:
    - `titles.poster_refreshed_at`
    - `episodes_cache.still_refreshed_at`
  - stale visible refresh теперь обновляет только ratings/details; artwork не уходит в частый polling
  - sync-driven artwork recheck работает только как targeted event path:
    - новый title/show после sync
    - новый episode / смена `next_episode`
    - manual repair / targeted repair path

## Что добавлено поверх базовой стабилизации

- для web добавлен встроенный `episode ratings matrix overlay`:
  - открывается по клику на `title-level` rating chip у show cards
  - работает в `History` и `Progress`
  - не уводит на отдельный route
  - не ломает patch-only refresh
- matrix overlay читает show episode metadata из shared SQLite `episodes_cache`
- open path для matrix overlay теперь `DB-first`:
  - если episode rows уже есть в SQLite, overlay открывается без обязательного network hydrate
  - forced refresh идет только по явному retry-path
- в matrix overlay доступны:
  - IMDb episode heatmap/matrix по сезонам и эпизодам
  - строка `AVG.` по сезонам
  - toggle `Hide season 0`
  - tooltip по ячейке:
    - episode title
    - `Sxx Exx`
    - IMDb votes
  - click по episode cell -> IMDb episode page, если у row есть `imdb_id`
- в `Settings` для web теперь есть несколько режимов artwork sync:
  - `Full Sync`
  - `Sync`
  - `Timeout Sync`
  - `Repair Sync`
- `Repair Sync` добавлен как отдельный path для проблемных artwork rows:
  - `poster checked_no_data / retryable_failure`
  - `still checked_no_data / retryable_failure`
  - идет через `manual_repair`, а не через обычный sync-event refresh

## Что важно помнить

- `desktop` и `web` делят одну SQLite и один core
- `History` и `Progress` теперь используют общий enrich-state model и общую queue
- `History` и `Progress` читают metadata из одних и тех же таблиц:
  - `titles`
  - `episodes_cache`
- matrix overlay для show-level ratings тоже читает из тех же shared tables, а не строит отдельный web-only cache
- decision о refresh теперь принимается не только по `status`, но и по refresh timestamps для ratings
- decision о refresh теперь централизованно принимается по:
  - `status`
  - `trigger`
  - `requested_parts`
  - `last_checked_at`
- file caches теперь только provider response caches, а не decision authority
- если что-то визуально выглядит не так, сначала надо проверять:
  - что лежит в SQLite
  - какой enrich status у row
  - какой refresh timestamp у ratings / poster / still row
  - не крутится ли queue в `retryable_failure`

## Как работать дальше

- не продолжать старые гипотезы вслепую
- сначала проверять фактическое состояние в `STATE.md`
- для архитектурных решений сначала проверять `ARCHITECTURE.md`
- для UI-правок обязательно делать локальный screenshot check
- screenshot check считается завершенным только если:
  - скрин действительно просмотрен и визуально сопоставлен с запросом / референсом
  - результат явно оценен, а не просто сохранен
  - при расхождении UI дорабатывается до повторной проверки
- если возникает новый баг, описывать его как отдельный дефект, а не смешивать с предыдущими ветками
## 2026 Search + Matrix Notes

- Search web cards now use shared SQLite-cached title metadata for poster / IMDb / Trakt values instead of relying only on raw Trakt search payload.
- Search `IMDb votes` sorting now runs on enriched IMDb values available at SSR time.
- Search poster images now use the same direct-image fallback pattern as `History` / `Progress`.
- Search show cards can open the same show-level episode ratings matrix overlay as `History` / `Progress`.
- Episode ratings matrix UI now renders the `AVG.` line as the first line of the matrix; `ALL` stays only in that average line.

## 2026 Episode Still Retry Notes

- Episode still retry is now release-aware and visibility-aware in shared core policy:
  - visible `checked_no_data` for recent released episodes retries every 5 minutes
  - non-visible page context / sync path for recent released episodes retries every 1 hour
  - old / unknown / unreleased episodes keep the long fallback TTL
- Web routes now use a dedicated non-visible trigger (`page_context`) for `nearby` and `page` buckets, so aggressive retry is reserved for real viewport-visible placeholders.

## 2026 Matrix Provider + Rating Rendering Notes

- Episode ratings matrix supports one-click source switching: `IMDb` (default) / `Trakt` / `My ★`.
- First switch to `Trakt` can request quick missing Trakt details refresh for unresolved episode rows.
- Matrix `ALL` excludes season `0`.
- In `Trakt` mode, unreleased episodes and zero-vote Trakt ratings are displayed as unrated (`?`) and excluded from Trakt averages.
- History title rating chips now render `n/a` when rating status is already resolved (`ready` / `checked_no_data`) but a concrete provider value is missing; `Loading` is kept for unresolved states only.
- Current sync scope note: history ratings sync currently imports `episode` / `show` / `movie` ratings; `season` ratings are outside that import path.
- History filter controls auto-apply: type and rated-only changes submit immediately, title text submits after debounce.
- History watch sync fetches the movie watch-history stream in addition to the general stream, because Trakt can expose watched movies there even when recent general history is episode-only.
- Search page metadata enrichment is background-only for initial render, and `/cached-image` tries to return newly fetched bytes before redirect fallback.
