from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import Thread

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import timezone_from_utc_offset
from trakt_tracker.infrastructure.artwork_cache import warm_image_urls
from trakt_tracker.web.viewmodels import (
    DEFAULT_SEARCH_SORT_MODE,
    SEARCH_SORT_MODES,
    normalize_search_sort_mode,
    normalize_title_type,
    saved_search_matches,
    sort_search_results,
)

SEARCH_PAGE_SIZE = 24


def register_catalog_routes(app, *, render, render_fragment, schedule_search_enrichment) -> None:
    @app.get("/search", response_class=HTMLResponse)
    async def search_page(
        request: Request,
        q: str = "",
        type: str = "show",
        sort: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        saved_state = services.catalog.load_last_search_state()
        requested_type = (type or "").strip().lower()
        title_type = normalize_title_type(type)
        query = q.strip()
        sort_mode = normalize_search_sort_mode(sort, services.catalog.get_search_sort_mode())
        services.catalog.set_search_sort_mode(sort_mode)

        results = []
        source_label = ""
        error_message = ""
        selected_type = requested_type if requested_type in {"all", "movie", "show"} else "show"

        if query:
            try:
                if saved_search_matches(saved_state, query, title_type):
                    results = list(saved_state.get("results", []))
                    source_label = "Local cached result set"
                    if schedule_search_enrichment(request.app, results=results, query=query, title_type=title_type):
                        source_label += " with background metadata refresh"
                else:
                    results = await asyncio.to_thread(services.catalog.search_titles, query, title_type)
                    source_label = "Fresh Trakt search"
                    if schedule_search_enrichment(request.app, results=results, query=query, title_type=title_type):
                        source_label += " with background metadata enrichment"
            except Exception as exc:
                error_message = str(exc)
        elif saved_state:
            results = list(saved_state.get("results", []))
            query = str(saved_state.get("query", "") or "").strip()
            effective_title_type = title_type
            if effective_title_type:
                results = [
                    item
                    for item in results
                    if normalize_title_type(getattr(item, "title_type", None)) == effective_title_type
                ]
            source_label = "Last saved search"
            if schedule_search_enrichment(request.app, results=results, query=query, title_type=effective_title_type):
                source_label += " with background metadata refresh"

        results = sort_search_results(results, sort_mode)
        current_page = max(1, page)
        offset = (current_page - 1) * SEARCH_PAGE_SIZE
        paged_results = results[offset:offset + SEARCH_PAGE_SIZE + 1]
        has_next = len(paged_results) > SEARCH_PAGE_SIZE
        paged_results = paged_results[:SEARCH_PAGE_SIZE]
        _attach_search_rating_badges(services, paged_results)
        return render(
            request,
            "search_v2.html",
            {
                "page_title": "Search",
                "page": current_page,
                "has_next": has_next,
                "query": query,
                "search_type": selected_type,
                "sort_mode": sort_mode or DEFAULT_SEARCH_SORT_MODE,
                "sort_modes": SEARCH_SORT_MODES,
                "results": paged_results,
                "search_history": services.catalog.search_history(),
                "source_label": source_label,
                "error_message": error_message,
            },
        )

    @app.get("/search/{title_type}/{trakt_id}/play")
    async def search_play(request: Request, title_type: str, trakt_id: int, title: str = "") -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        normalized_type = normalize_title_type(title_type)
        if normalized_type is None:
            return RedirectResponse(url="/search", status_code=302)
        target_title = title.strip()
        if not target_title:
            try:
                title_item = await asyncio.to_thread(services.catalog.get_title_details, trakt_id, normalized_type)
                target_title = title_item.title
            except Exception:
                target_title = ""
        target_url = await asyncio.to_thread(services.play.resolve_kinopoisk_url, target_title)
        if target_url:
            services.operations.publish("Play", f"Search play requested: {target_title or trakt_id}")
            return RedirectResponse(url=target_url, status_code=302)
        return RedirectResponse(url="/search", status_code=302)

    @app.get("/titles/{title_type}/{trakt_id}", response_class=HTMLResponse)
    async def title_details_page(request: Request, title_type: str, trakt_id: int) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        normalized_type = normalize_title_type(title_type)
        if normalized_type is None:
            return render(
                request,
                "details.html",
                {
                    "page_title": "Title details",
                    "title_item": None,
                    "error_message": "Unsupported title type.",
                },
                status_code=404,
            )

        try:
            title_item = await asyncio.to_thread(services.catalog.get_title_details, trakt_id, normalized_type)
            return render(
                request,
                "details.html",
                {
                    "page_title": title_item.title,
                    "title_item": title_item,
                    "error_message": "",
                },
            )
        except Exception as exc:
            return render(
                request,
                "details.html",
                {
                    "page_title": "Title details",
                    "title_item": None,
                    "error_message": str(exc),
                },
                status_code=502,
            )

    @app.get("/titles/show/{trakt_id}/episode-ratings-matrix", response_class=HTMLResponse)
    async def title_episode_ratings_matrix(
        request: Request,
        trakt_id: int,
        refresh: str = "",
        provider: str = "imdb",
        refresh_missing: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        force_refresh = str(refresh or "").strip().lower() in {"1", "true", "yes", "on"}
        should_refresh_missing = str(refresh_missing or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            matrix = await asyncio.to_thread(
                services.episode_ratings_matrix.load_show_matrix,
                trakt_id,
                force_refresh=force_refresh,
                provider=provider,
                refresh_missing=should_refresh_missing,
            )
            return HTMLResponse(
                render_fragment(
                    request,
                    "title_episode_ratings_matrix.html",
                    {
                        "matrix": matrix,
                    },
                )
            )
        except Exception as exc:
            return HTMLResponse(
                render_fragment(
                    request,
                    "title_episode_ratings_matrix.html",
                    {
                        "matrix": None,
                        "matrix_error_message": str(exc),
                        "matrix_trakt_id": trakt_id,
                    },
                ),
                status_code=500,
            )

    @app.get("/search/show/{trakt_id}/watch-panel", response_class=HTMLResponse)
    async def search_show_watch_panel(request: Request, trakt_id: int, season: int | None = None) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        try:
            if season is None:
                panel = await asyncio.to_thread(services.search_watch.load_show_panel, trakt_id)
            else:
                panel = await asyncio.to_thread(services.search_watch.load_show_panel, trakt_id, season)
            _warm_default_season_stills_in_background(getattr(request.app.state, "image_cache", None), panel)
            return HTMLResponse(
                render_fragment(
                    request,
                    "search_show_watch_panel.html",
                    {
                        "panel": panel,
                    },
                )
            )
        except Exception as exc:
            return HTMLResponse(
                render_fragment(
                    request,
                    "search_show_watch_panel.html",
                    {
                        "panel": None,
                        "error_message": str(exc),
                        "trakt_id": trakt_id,
                    },
                ),
                status_code=500,
            )

    @app.post("/search/watch")
    async def search_watch(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("title_type", "") or "")) or "movie"
        scope = str(payload.get("scope", "") or "title").strip().lower()
        if scope not in {"title", "season", "episode"}:
            scope = "title"
        try:
            trakt_id = int(payload.get("trakt_id") or 0)
        except (TypeError, ValueError):
            trakt_id = 0
        if trakt_id <= 0:
            return JSONResponse({"ok": False, "message": "Missing Trakt id."}, status_code=400)
        season = _optional_int(payload.get("season"))
        episode = _optional_int(payload.get("episode"))
        title = str(payload.get("title", "") or "").strip()
        try:
            watched_at = _parse_search_watched_at(
                payload,
                utc_offset=services.auth.config.utc_offset,
            )
            count = await asyncio.to_thread(
                services.search_watch.mark_watch,
                title_type=title_type,
                trakt_id=trakt_id,
                title=title,
                scope=scope,
                season=season,
                episode=episode,
                watched_at=watched_at,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        services.operations.publish("Search action", f"Marked watched from search: {title or title_type} ({count})")
        return JSONResponse(
            {
                "ok": True,
                "message": f"Marked {count} item{'s' if count != 1 else ''} watched.",
                "count": count,
            }
        )


def _optional_int(value) -> int | None:
    try:
        raw = str(value if value is not None else "").strip()
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _attach_search_rating_badges(services: ServiceContainer, results: list) -> None:
    try:
        badges = services.history.title_rating_badges([int(getattr(item, "trakt_id", 0) or 0) for item in results])
    except Exception:
        return
    for item in results:
        rating = badges.get(int(getattr(item, "trakt_id", 0) or 0))
        if rating is not None:
            item.title_episode_avg_rating = rating


def _warm_default_season_stills_in_background(cache, panel) -> None:
    if cache is None:
        return
    urls = []
    for season in getattr(panel, "seasons", []):
        if not getattr(season, "is_default", False):
            continue
        for episode in getattr(season, "episodes", []):
            still_url = str(getattr(episode, "still_url", "") or "")
            if still_url:
                urls.append(still_url)
        break
    if not urls:
        return
    Thread(
        target=lambda: warm_image_urls(cache, urls, timeout=15, max_workers=4),
        daemon=True,
    ).start()


def _parse_search_watched_at(payload: dict, *, utc_offset: str) -> datetime | None:
    date_mode = str(payload.get("date_mode", "") or "none").strip().lower()
    if date_mode == "none":
        raise ValueError("Undated watched history is not supported by Trakt sync.")
    if date_mode == "custom":
        raw = str(payload.get("watched_at", "") or "").strip()
        if not raw:
            raise ValueError("Choose a custom watch date or use another date mode.")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone_from_utc_offset(utc_offset))
        return parsed.astimezone(UTC)
    return datetime.now(tz=UTC)
