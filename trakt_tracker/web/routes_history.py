from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import OperationalError

from trakt_tracker.application.enrich_queue import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DROPPED,
    TASK_STATUS_FAILED,
    build_history_episode_task,
    build_history_title_task,
)
from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import timezone_from_utc_offset
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.viewmodels import HISTORY_PAGE_SIZE, normalize_title_type


def register_history_routes(app, *, render, render_fragment) -> None:
    @app.get("/history", response_class=HTMLResponse)
    async def history_page(
        request: Request,
        type: str = "all",
        title: str = "",
        page: int = 1,
        flash: str = "",
        rate_trakt_id: int | None = None,
        rate_type: str = "",
        rate_season: int | None = None,
        rate_episode: int | None = None,
        rate_title: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        title_type = normalize_title_type(type)
        current_page = max(1, page)
        title_filter = title.strip() or None
        rows, has_next, grouped_days = _load_history_page_data(
            services,
            title_type=title_type,
            title_filter=title_filter,
            current_page=current_page,
        )
        title_options = services.history.history_titles(title_type=title_type)
        return render(
            request,
            "history.html",
            {
                "page_title": "History",
                "history_rows": rows,
                "history_days": grouped_days,
                "history_type": title_type or "all",
                "history_title_filter": title.strip(),
                "history_title_options": title_options,
                "page": current_page,
                "has_next": has_next,
                "rate_trakt_id": rate_trakt_id,
                "rate_type": normalize_title_type(rate_type) or "",
                "rate_season": rate_season,
                "rate_episode": rate_episode,
                "rate_title": rate_title,
                "history_sync_running": request.app.state.bg_tasks.is_running("history_sync"),
                "flash": flash,
            },
        )

    @app.get("/history/auto-sync")
    async def history_auto_sync(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        if bg_tasks.is_running("history_sync") or services.enrich_queue.is_running():
            return JSONResponse({"changed": False, "message": "History auto-sync: busy."})
        try:
            changed = await asyncio.to_thread(services.sync.maybe_refresh_history)
        except OperationalError as exc:
            if "database is locked" in str(exc).lower():
                return JSONResponse({"changed": False, "message": "History auto-sync: busy."})
            return JSONResponse({"changed": False, "error": str(exc), "message": f"History auto-sync failed: {exc}"})
        except Exception as exc:
            return JSONResponse({"changed": False, "error": str(exc), "message": f"History auto-sync failed: {exc}"})
        return JSONResponse(
            {
                "changed": bool(changed),
                "message": "History auto-sync updated rows." if changed else "History auto-sync: no changes.",
            }
        )

    @app.post("/history/refresh")
    async def history_refresh(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("type", "all") or "all"))
        title_filter_raw = str(payload.get("title_filter", "") or "")
        title_filter = title_filter_raw.strip() or None
        try:
            current_page = max(1, int(payload.get("page", 1) or 1))
        except (TypeError, ValueError):
            current_page = 1
        raw_visible_title_keys = payload.get("visible_title_keys", [])
        viewport_title_keys = _normalize_title_keys(payload.get("viewport_title_keys", []))
        nearby_title_keys = _normalize_title_keys(payload.get("nearby_title_keys", []))
        page_title_keys = _normalize_title_keys(payload.get("page_title_keys", []))
        if not page_title_keys:
            page_title_keys = _normalize_title_keys(raw_visible_title_keys)
        try:
            queue_after_revision = max(0, int(payload.get("queue_after_revision", 0) or 0))
        except (TypeError, ValueError):
            queue_after_revision = 0
        rows, has_next, grouped_days = _load_history_page_data(
            services,
            title_type=title_type,
            title_filter=title_filter,
            current_page=current_page,
        )
        current_title_groups = _title_group_map(grouped_days)
        current_page_keys = list(current_title_groups.keys())
        rows_by_title_key = _rows_by_title_key(rows, services.auth.config.utc_offset)
        if not request.app.state.bg_tasks.is_running("history_sync"):
            services.enrich_queue.submit_history_refresh(
                viewport_tasks=_build_history_bucket_tasks(services, rows_by_title_key, viewport_title_keys, priority=1),
                nearby_tasks=_build_history_bucket_tasks(services, rows_by_title_key, nearby_title_keys, priority=2),
                page_tasks=_build_history_bucket_tasks(services, rows_by_title_key, page_title_keys, priority=3),
            )
        relevant_title_keys = set(page_title_keys or current_page_keys)
        queue = services.enrich_queue.list_updates(
            after_revision=queue_after_revision,
            relevant_title_keys=relevant_title_keys,
        )
        missing_title_keys = [key for key in page_title_keys if key not in current_title_groups]
        affected_title_keys = []
        for update in queue.get("updates", []):
            if update.get("status") not in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_DROPPED}:
                continue
            for title_key in update.get("affected_title_keys", []):
                if title_key in current_title_groups and title_key not in affected_title_keys:
                    affected_title_keys.append(title_key)
        rendered_groups = []
        for title_key in affected_title_keys:
            title_group = current_title_groups.get(title_key)
            if title_group is None:
                continue
            rendered_groups.append(
                {
                    "title_key": title_key,
                    "html": render_fragment(
                        request,
                        "history_title_card.html",
                        {
                            "title_group": title_group,
                            "history_type": title_type or "all",
                            "history_title_filter": title_filter_raw,
                            "page": current_page,
                        },
                    ),
                }
            )
        return JSONResponse(
            {
                "title_groups": rendered_groups,
                "missing_title_keys": missing_title_keys,
                "history_sync_running": request.app.state.bg_tasks.is_running("history_sync"),
                "queue": queue,
                "page_changed": bool(page_title_keys) and page_title_keys != current_page_keys,
                "has_next": has_next,
            }
        )

    @app.post("/history/sync")
    async def history_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        form = await request.form()
        history_type = normalize_title_type(str(form.get("type", "all") or "all")) or "all"
        title_filter = str(form.get("title_filter", "") or "")
        try:
            page = max(1, int(str(form.get("page", "1") or "1")))
        except ValueError:
            page = 1
        if services.enrich_queue.is_running():
            flash = "History sync is waiting for current enrich tasks to finish."
            redirect_url = f"/history?type={history_type}&title={quote(title_filter)}&page={page}&flash={quote(flash)}"
            return RedirectResponse(url=redirect_url, status_code=303)
        started = bg_tasks.start(
            "history_sync",
            source="History sync (manual full)",
            operations=services.operations,
            fn=services.sync.refresh_history,
        )
        flash = "History sync started." if started else "History sync is already running."
        redirect_url = f"/history?type={history_type}&title={quote(title_filter)}&page={page}&flash={quote(flash)}"
        return RedirectResponse(url=redirect_url, status_code=303)

    @app.post("/history/rate")
    async def history_rate(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        history_type = normalize_title_type(str(form.get("type", "all") or "all")) or "all"
        title_filter = str(form.get("title_filter", "") or "")
        try:
            page = max(1, int(str(form.get("page", "1") or "1")))
        except ValueError:
            page = 1
        trakt_id = int(str(form.get("trakt_id", "0") or "0"))
        rating_type = normalize_title_type(str(form.get("rating_type", "") or "")) or "movie"
        season_raw = str(form.get("season", "") or "").strip()
        episode_raw = str(form.get("episode", "") or "").strip()
        season = int(season_raw) if season_raw else None
        episode = int(episode_raw) if episode_raw else None
        title = str(form.get("title_value", "") or "")
        rating = int(str(form.get("rating", "0") or "0"))
        flash = "Rating saved."
        services.operations.publish("History action", f"Save rating: {title} -> {rating}/10")
        try:
            services.interactions.save_rating(
                RatingInput(
                    title_type=rating_type,
                    trakt_id=trakt_id,
                    rating=rating,
                    season=season,
                    episode=episode,
                ),
                title=title,
            )
        except Exception as exc:
            flash = f"Rating failed: {exc}"
        redirect_url = f"/history?type={history_type}&title={quote(title_filter)}&page={page}&flash={quote(flash)}"
        return RedirectResponse(url=redirect_url, status_code=303)


def _load_history_page_data(
    services: ServiceContainer,
    *,
    title_type: str | None,
    title_filter: str | None,
    current_page: int,
) -> tuple[list[dict], bool, list[dict]]:
    rows = services.history.history(
        title_type=title_type,
        title_filter=title_filter,
        limit=HISTORY_PAGE_SIZE + 1,
        offset=(current_page - 1) * HISTORY_PAGE_SIZE,
    )
    has_next = len(rows) > HISTORY_PAGE_SIZE
    rows = rows[:HISTORY_PAGE_SIZE]
    grouped_days = _group_history_rows(rows, services.auth.config.utc_offset)
    return rows, has_next, grouped_days


def _normalize_title_keys(raw_keys) -> list[str]:
    return [
        str(key)
        for key in dict.fromkeys(raw_keys if isinstance(raw_keys, list) else [])
        if isinstance(key, str) and key.strip()
    ]


def _rows_by_title_key(rows: list[dict], utc_offset: str) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for row in rows:
        title_key = _history_title_key_for_row(row, utc_offset)
        result.setdefault(title_key, []).append(row)
    return result


def _build_history_bucket_tasks(
    services: ServiceContainer,
    rows_by_title_key: dict[str, list[dict]],
    title_keys: list[str],
    *,
    priority: int,
) -> list:
    tasks: list = []
    for title_key in title_keys:
        title_rows = rows_by_title_key.get(title_key, [])
        if not title_rows:
            continue
        for trakt_id, title_type in services.catalog.select_title_enrich_keys(title_rows):
            tasks.append(
                build_history_title_task(
                    title_key=title_key,
                    trakt_id=trakt_id,
                    title_type=title_type,
                    priority=priority,
                )
            )
        for show_trakt_id, season, episode in services.history.select_episode_enrich_keys(title_rows):
            tasks.append(
                build_history_episode_task(
                    title_key=title_key,
                    show_trakt_id=show_trakt_id,
                    season=season,
                    episode=episode,
                    priority=priority,
                )
            )
    return tasks


def _title_group_map(grouped_days: list[dict]) -> OrderedDict[str, dict]:
    result: OrderedDict[str, dict] = OrderedDict()
    for day in grouped_days:
        for title_group in day.get("title_groups", []):
            title_key = str(title_group.get("title_key", "") or "")
            if title_key:
                result[title_key] = title_group
    return result


def _group_history_rows(rows: list[dict], utc_offset: str) -> list[dict]:
    tz = timezone_from_utc_offset(utc_offset)
    groups: OrderedDict[str, dict] = OrderedDict()
    for row in rows:
        watched_at = row.get("watched_at")
        if watched_at is None:
            day_label = "Unknown date"
        else:
            normalized = watched_at if watched_at.tzinfo is not None else watched_at.replace(tzinfo=UTC)
            local_dt = normalized.astimezone(tz)
            day_label = local_dt.strftime("%d.%m.%Y")
        group = groups.setdefault(day_label, {"day_label": day_label, "count": 0, "title_groups": [], "_title_map": OrderedDict()})
        group["count"] += 1
        title_key = (row.get("type"), row.get("title_trakt_id"))
        title_group = group["_title_map"].get(title_key)
        if title_group is None:
            title_group = {
                "title_key": _history_title_key_for_day(day_label, row.get("type", ""), row.get("title_trakt_id")),
                "title_trakt_id": row.get("title_trakt_id"),
                "title": row.get("title", ""),
                "title_slug": row.get("title_slug", ""),
                "type": row.get("type", ""),
                "poster_url": row.get("poster_url", ""),
                "title_poster_status": row.get("title_poster_status", "unknown"),
                "title_trakt_rating": row.get("title_trakt_rating"),
                "title_trakt_votes": row.get("title_trakt_votes"),
                "title_imdb_rating": row.get("title_imdb_rating"),
                "title_imdb_votes": row.get("title_imdb_votes"),
                "title_ratings_status": row.get("title_ratings_status", "unknown"),
                "title_episode_avg_rating": row.get("title_episode_avg_rating"),
                "title_episode_rated_count": row.get("title_episode_rated_count", 0),
                "entries": [],
            }
            group["_title_map"][title_key] = title_group
            group["title_groups"].append(title_group)
        title_group["entries"].append(row)
    for day_group in groups.values():
        day_group.pop("_title_map", None)
    return list(groups.values())


def _history_title_key_for_day(day_label: str, title_type: str, title_trakt_id) -> str:
    return f"{day_label}:{title_type}:{title_trakt_id}"


def _history_title_key_for_row(row: dict, utc_offset: str) -> str:
    watched_at = row.get("watched_at")
    if watched_at is None:
        day_label = "Unknown date"
    else:
        tz = timezone_from_utc_offset(utc_offset)
        normalized = watched_at if watched_at.tzinfo is not None else watched_at.replace(tzinfo=UTC)
        day_label = normalized.astimezone(tz).strftime("%d.%m.%Y")
    return _history_title_key_for_day(day_label, row.get("type", ""), row.get("title_trakt_id"))
