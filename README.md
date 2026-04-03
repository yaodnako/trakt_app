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

1. `README.md`
2. `ARCHITECTURE.md`
3. `STATE.md`
4. `FEATURES.md`

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

## Что важно помнить

- `desktop` и `web` делят одну SQLite и один core
- `History` и `Progress` теперь используют общий enrich-state model и общую queue
- `History` и `Progress` читают metadata из одних и тех же таблиц:
  - `titles`
  - `episodes_cache`
- file caches теперь только provider response caches, а не decision authority
- если что-то визуально выглядит не так, сначала надо проверять:
  - что лежит в SQLite
  - какой enrich status у row
  - не крутится ли queue в `retryable_failure`

## Как работать дальше

- не продолжать старые гипотезы вслепую
- сначала проверять фактическое состояние в `STATE.md`
- для архитектурных решений сначала проверять `ARCHITECTURE.md`
- для UI-правок обязательно делать локальный screenshot check
- если возникает новый баг, описывать его как отдельный дефект, а не смешивать с предыдущими ветками
