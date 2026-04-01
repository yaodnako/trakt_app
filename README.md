# Trakt Tracker

Краткая точка входа для нового чата и новой сессии по проекту.

## Что это сейчас

- `desktop` на `PySide6` остаётся основным рабочим интерфейсом
- `web` на `FastAPI + Jinja2` развивается как второй UI поверх того же Python core и той же SQLite
- source of truth:
  - `Trakt` для history / ratings / progress / calendar
  - `TMDb` для artwork и части metadata
  - official `IMDb datasets` для IMDb ratings/votes

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

## Где проект сейчас по архитектуре

Уже вынесены отдельные слои:

- [sync_policy.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/sync_policy.py)
- [operations.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/operations.py)
- [history_sync.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history_sync.py)
- [progress_sync.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/progress_sync.py)
- [notification_refresh.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/notification_refresh.py)
- [catalog.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/catalog.py)
- [history.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history.py)
- [interactions.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/interactions.py)
- [history_read_model.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/history_read_model.py)
- [episode_metadata.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/episode_metadata.py)
- [trakt_payload_cache.py](/D:/CodexProjects/Trakt_app/trakt_tracker/application/trakt_payload_cache.py)

Это уже лучше, чем исходный монолитный `services.py`, но рефактор ещё не завершён.

## Что важно помнить

- `desktop` и `web` делят одну SQLite и один core
- sync / ratings / progress всё ещё чувствительные места
- при вопросах про “почему что-то не обновилось” сначала смотреть `STATE.md`
- при вопросах про дальнейший рефактор сначала смотреть `ARCHITECTURE.md`
