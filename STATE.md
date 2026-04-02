# Trakt Tracker State

## Текущее фактическое состояние

- есть локальный git baseline commit
- основной remote подключён
- текущий рефактор уже разбил часть sync/use-case логики на отдельные файлы
- web теперь принят как основной рабочий UI, desktop больше не считается главным baseline

## Что уже реально сделано

- background manual sync в web
- background manual history sync в desktop
- `Debug mode` в desktop/web
- `Progress` и `History` опираются на более явную policy/update схему
- уведомления ограничены текущим `next episode`, а не любым левым calendar special
- для web заведен локальный screenshot workflow через Playwright/Chrome 125% view для самопроверки UI
- вынесены:
  - `CatalogService`
  - `HistoryService`
  - `InteractionService`
  - `HistorySyncWorkflow`
  - `ProgressSyncWorkflow`
  - `NotificationRefreshWorkflow`
  - `HistoryReadModelService`
  - `EpisodeMetadataService`
  - `SyncPolicy`
  - `OperationLog`

## Что ещё не доведено

- search state / search history / title-details orchestration уже вынесены из `LibraryService`
- history/rating write-side и history read facade уже отделены от прежнего `LibraryService`
- watch/seen/rating/drop action flow больше не размазан напрямую между desktop/web
- IMDb dataset теперь может обновляться в фоне по настраиваемому interval и в desktop, и в web
- history web теперь умеет lazy-enrich visible rows:
  - Trakt episode aggregate rating/votes
  - missing title posters
- `MainWindow` всё ещё перегружен
- web routes уже частично разрезаны по screen-level modules, `web/app.py` стал тоньше
- `Debug mode` ещё не до конца унифицирован
- refresh policy для episode details/ratings всё ещё можно упростить и сделать яснее

## Подтверждённые проблемные зоны

- часть UX-проблем раньше маскировалась логикой sync/cache, а не была чисто UI-багом
- stale ratings / stale next-episode metadata были реальной проблемой
- auth/token refresh path был неполным, из-за чего могли лететь `401`
- web debug раньше переигрывал старый хвост событий

## Что НЕ считать законченным

Не считать проект полностью архитектурно дочищенным.

Сейчас это состояние:

- уже не хаотичный монолит
- но ещё не чистая финальная структура

## Следующий новый чат

В новом чате стартовать так:

1. прочитать `README.md`
2. прочитать `ARCHITECTURE.md`
3. прочитать `STATE.md`
4. потом смотреть `FEATURES.md`

И только после этого продолжать рефактор.
