# Trakt Tracker State

## Текущее фактическое состояние

- есть локальный git baseline commit
- основной remote подключен
- `web` принят как основной рабочий UI
- `desktop` остается вторым UI поверх того же core и SQLite

## Что уже реально сделано

- большой stabilization/refactor план для `History + Progress` проведен до конца по фазам 1–5
- введен explicit enrich-state model в SQLite
- sync / enrich / UI refresh разведены лучше, чем раньше
- `History` больше не должен сходиться через full reload страницы
- `Progress` теперь использует тот же shared enrich core, что и `History`
- введена shared visible-first queue для provider-backed metadata
- `History` и `Progress` читают одни и те же shared metadata tables
- добавлен и используется локальный screenshot workflow для web UI

## Что сейчас считается нормой

### History

- grouped-by-day layout в web
- title-level poster / title-level ratings chips
- episode still / episode ratings
- queue-driven patch refresh без whole-page reload
- stable loading / empty states:
  - `Loading`
  - `No poster`
  - `No preview`
  - `n/a` только для terminal `checked_no_data`

### Progress

- queue-driven patch refresh без whole-page reload
- shared title/episode metadata из SQLite
- title poster / provider chips / next-episode preview
- stable loading / empty states по тем же правилам, что и в `History`

## Подтвержденные проблемные зоны, которые уже были

Вот какие реальные проблемы уже всплывали и фиксились в этом цикле:

- reload-driven convergence в `History`
- queue retry-loop на `retryable_failure`
- image proxy stall на `/cached-image`
- неверное скрытие уже известных values из-за слишком жесткой привязки template к status
- визуальный конфликт карточек `History`, когда один и тот же title повторялся в разных днях
- отсутствие повторной проверки после local re-rate

## Что еще не стоит считать “идеальной финальной архитектурой”

Проект уже не в промежуточном монолитном состоянии, но это все еще не “последняя полировка всего продукта”.

Что еще остается как обычная будущая работа, а не как незавершенный blocker фаз 1–5:

- further cleanup desktop orchestration
- further cleanup debug UX
- точечные UX-polish задачи на экранах
- transport/network hardening around provider failures

## Как начинать новый чат после этого цикла

В новом чате стартовать так:

1. прочитать `README.md`
2. прочитать `ARCHITECTURE.md`
3. прочитать `STATE.md`
4. потом смотреть `FEATURES.md`

И только после этого продолжать работу.

Если баг новый:

- формулировать его как отдельный текущий дефект
- не смешивать с предыдущими ветками расследования
