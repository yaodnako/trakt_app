# Trakt Tracker

This is the single entry-point document for a new chat or a new development session.

## Start Here

If you are starting a new chat for this project:

1. Read this `README.md` first.
2. Then open only these files as needed:
   - `ARCHITECTURE.md`
     For development rules, architectural boundaries, workflow, and documentation ownership.
   - `FEATURES.md`
     For what the app currently does and what is only partial.
   - `STATE.md`
     For current technical state, known limitations, and confirmed upstream data gaps.

## Which File To Use

- Use `README.md` for:
  - setup
  - launch
  - first orientation
- Use `ARCHITECTURE.md` for:
  - how to work in this repo
  - where logic should live
  - when to update docs
- Use `FEATURES.md` for:
  - current feature inventory
  - what should be preserved if migrating stacks
- Use `STATE.md` for:
  - current project reality
  - confirmed provider/data issues
  - known limitations and cleanup notes

See `ARCHITECTURE.md` for development rules, project structure, and documentation ownership.
See `FEATURES.md` for current functionality inventory.
See `STATE.md` for current technical state and known limitations.

Current project direction:
- keep the existing desktop UI as the working baseline
- evaluate a web prototype next instead of extending PySide blindly
- build the web prototype over the existing Python services/repositories and SQLite state

Локальное Windows desktop приложение для учета просмотра фильмов и сериалов через Trakt API.

## Возможности v1

- OAuth-авторизация через Trakt
- Поиск фильмов и сериалов
- Карточки тайтлов
- Оценки `1..10`
- Добавление просмотров в историю
- Прогресс сериалов и upcoming эпизоды
- Локальная история действий
- Windows toast уведомления

## Запуск

1. Установить зависимости:

```powershell
python -m pip install -e .
```

2. Запустить приложение:

```powershell
python -m trakt_tracker.main
```

## Web Prototype

Desktop remains the baseline runtime.

The first web prototype is intentionally narrow:
- `Progress`
- `History`
- `Search`
- title/details page

It reuses the existing Python service layer and the same SQLite database instead of replacing repositories/services.
`Progress` in the web shell now supports:
- shared local progress cards
- `Sync`
- `Hide Upcoming`
- `Show Dropped`
- `Play`
- `Watched`
- `Drop` / `Undrop`
- immediate rating-or-skip flow after marking watched

Install web dependencies:

```powershell
python -m pip install -e ".[web]"
```

Run the web prototype:

```powershell
python -m trakt_tracker.web.main
```

Startup timing is written to:

`C:\Users\yaodn\AppData\Local\TraktTracker\web_startup.log`

Per-request timing is written to:

`C:\Users\yaodn\AppData\Local\TraktTracker\web_request_timings.log`

## Настройка Trakt

Создайте собственное приложение в Trakt и укажите redirect URI:

`http://127.0.0.1:8765/callback`

## Startup Timing

The app writes the latest startup timing breakdown to:

`C:\Users\yaodn\AppData\Local\TraktTracker\startup.log`

This log shows startup stages and elapsed milliseconds for each stage.

При первом запуске приложение попросит `client_id` и `client_secret`, затем откроет браузер для OAuth.
