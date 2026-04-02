from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import timezone_from_utc_offset
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.viewmodels import HISTORY_PAGE_SIZE, normalize_title_type


def register_history_routes(app, *, render) -> None:
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
        rows = services.history.history(
            title_type=title_type,
            title_filter=title_filter,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=(current_page - 1) * HISTORY_PAGE_SIZE,
        )
        has_next = len(rows) > HISTORY_PAGE_SIZE
        rows = rows[:HISTORY_PAGE_SIZE]
        enrich_initial_seq = services.operations.current_seq()
        enrich_started = False
        if services.history.has_missing_visible_episode_details(rows):
            enrich_started = request.app.state.bg_tasks.start(
                f"history_enrich:{title_type or 'all'}:{title_filter or ''}:{current_page}",
                source="History episode enrich",
                operations=services.operations,
                fn=lambda rows_snapshot=list(rows): services.history.enrich_visible_episode_details(rows_snapshot),
            )
        title_enrich_started = request.app.state.bg_tasks.start(
            f"history_title_enrich:{title_type or 'all'}:{title_filter or ''}:{current_page}",
            source="History title enrich",
            operations=services.operations,
            fn=lambda rows_snapshot=list(rows): services.catalog.enrich_visible_titles(rows_snapshot),
        )
        grouped_days = _group_history_rows(rows, services.auth.config.utc_offset)
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
                "history_enrich_started": enrich_started,
                "history_title_enrich_started": title_enrich_started,
                "history_enrich_initial_seq": enrich_initial_seq,
                "flash": flash,
            },
        )

    @app.get("/history/auto-sync")
    async def history_auto_sync(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            changed = await asyncio.to_thread(services.sync.maybe_refresh_history)
        except Exception as exc:
            return JSONResponse({"changed": False, "error": str(exc), "message": f"History auto-sync failed: {exc}"})
        return JSONResponse(
            {
                "changed": bool(changed),
                "message": "History auto-sync updated rows." if changed else "History auto-sync: no changes.",
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
        group = groups.setdefault(day_label, {"day_label": day_label, "count": 0, "entries": []})
        group["count"] += 1
        group["entries"].append(row)
    return list(groups.values())
