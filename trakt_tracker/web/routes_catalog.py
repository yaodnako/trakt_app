from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trakt_tracker.application.enrich_queue import build_history_episode_task, build_show_episode_hydration_task
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_STILL,
    TRIGGER_PAGE_CONTEXT,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
)
from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import timezone_from_utc_offset
from trakt_tracker.infrastructure.artwork_cache import tmdb_episode_preview_url
from trakt_tracker.web.viewmodels import (
    DEFAULT_SEARCH_SORT_MODE,
    SEARCH_SORT_MODES,
    WATCHLIST_SORT_MODES,
    normalize_search_sort_mode,
    normalize_title_type,
    normalize_watchlist_sort_mode,
    sort_search_results,
    sort_watchlist_results,
    format_release_distance,
)

SEARCH_PAGE_SIZE = 24
EXPLORE_FEEDS = {"anticipated", "trending", "popular"}


def _normalize_rating_threshold(value: str) -> float | None:
    try:
        threshold = float((value or "").strip())
    except (TypeError, ValueError):
        return None
    return threshold if 0 <= threshold <= 10 else None


def _format_rating_threshold(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _saved_or_query_flag(request: Request, name: str, saved_value: bool) -> bool:
    if name not in request.query_params:
        return bool(saved_value)
    return any(
        str(value or "").strip().lower() in {"1", "true", "yes", "on"}
        for value in request.query_params.getlist(name)
    )


def _schedule_watchlist_snapshot_refresh(request: Request, services: ServiceContainer) -> None:
    has_snapshot = getattr(services.catalog, "has_watchlist_snapshot", None)
    refresh_snapshot = getattr(services.catalog, "refresh_watchlist_snapshot", None)
    if not callable(has_snapshot) or not callable(refresh_snapshot):
        return
    try:
        if has_snapshot():
            return
    except Exception:
        return
    request.app.state.bg_tasks.start(
        "watchlist_snapshot",
        source="Watchlist snapshot",
        operations=services.operations,
        fn=refresh_snapshot,
    )


def register_catalog_routes(app, *, render, render_fragment, schedule_search_enrichment) -> None:
    @app.get("/search", response_class=HTMLResponse)
    async def search_page(
        request: Request,
        q: str = "",
        type: str = "all",
        sort: str = "",
        page: int = 1,
        imdb_min: str = "",
        trakt_min: str = "",
        catalog_shell: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        saved_state = services.catalog.load_last_search_state()
        requested_type = (type or "").strip().lower()
        title_type = normalize_title_type(type)
        query = q.strip()
        catalog_loading = str(catalog_shell or "").strip().lower() in {"1", "true", "yes", "on"}
        if catalog_loading and not query and saved_state:
            query = str(saved_state.get("query", "") or "").strip()
            if "type" not in request.query_params:
                requested_type = str(saved_state.get("title_type", "all") or "all").strip().lower()
                title_type = normalize_title_type(requested_type)
        saved_filters = services.catalog.load_search_rating_filters()
        effective_imdb_min = imdb_min if "imdb_min" in request.query_params else saved_filters.get("imdb_min", "")
        effective_trakt_min = trakt_min if "trakt_min" in request.query_params else saved_filters.get("trakt_min", "")
        selected_imdb_min = _normalize_rating_threshold(effective_imdb_min)
        selected_trakt_min = _normalize_rating_threshold(effective_trakt_min)
        selected_hide_watchlisted = _saved_or_query_flag(
            request,
            "hide_watchlisted",
            bool(saved_filters.get("hide_watchlisted", False)),
        )
        selected_hide_history = _saved_or_query_flag(
            request,
            "hide_history",
            bool(saved_filters.get("hide_history", False)),
        )
        services.catalog.save_search_rating_filters(
            _format_rating_threshold(selected_imdb_min),
            _format_rating_threshold(selected_trakt_min),
            hide_watchlisted=selected_hide_watchlisted,
            hide_history=selected_hide_history,
        )
        sort_mode = normalize_search_sort_mode(sort, services.catalog.get_search_sort_mode())
        services.catalog.set_search_sort_mode(sort_mode)
        selected_type = requested_type if requested_type in {"all", "movie", "show"} else "all"
        _schedule_watchlist_snapshot_refresh(request, services)

        if catalog_loading:
            return render(
                request,
                "search_v2.html",
                {
                    "page_title": "Search", "page": max(1, page), "has_next": False, "page_count": max(1, page),
                    "query": query, "search_type": selected_type, "sort_mode": sort_mode or DEFAULT_SEARCH_SORT_MODE,
                    "sort_modes": SEARCH_SORT_MODES, "results": [], "search_history": services.catalog.search_history(),
                    "source_label": "", "error_message": "", "page_kind": "search",
                    "search_imdb_min": _format_rating_threshold(selected_imdb_min),
                    "search_trakt_min": _format_rating_threshold(selected_trakt_min),
                    "search_hide_watchlisted": selected_hide_watchlisted, "search_hide_history": selected_hide_history,
                    "catalog_loading": True,
                },
            )

        results = []
        source_label = ""
        error_message = ""
        current_page = max(1, page)
        page_count = current_page
        provider_paged = False
        has_next = False
        watchlist_keys: set[tuple[str, int]] = set()
        watchlist_keys_loaded = False

        if query:
            try:
                watchlist_keys = await asyncio.to_thread(services.catalog.watchlist_keys, title_type=title_type)
                watchlist_keys_loaded = True
                history_keys = await asyncio.to_thread(services.catalog.history_keys)
                excluded_keys = set()
                if selected_hide_watchlisted:
                    excluded_keys.update(watchlist_keys)
                if selected_hide_history:
                    excluded_keys.update(history_keys)
                result_page = await asyncio.to_thread(
                    services.catalog.filtered_search_titles,
                    query,
                    title_type,
                    page=current_page,
                    limit=SEARCH_PAGE_SIZE,
                    imdb_min=selected_imdb_min,
                    trakt_min=selected_trakt_min,
                    max_scan_pages=max(1, int(getattr(services.auth.config, "explore_imdb_scan_page_limit", 10))),
                    excluded_keys=excluded_keys,
                )
                results = result_page.items
                current_page = result_page.page
                page_count = result_page.page_count
                has_next = current_page < page_count
                provider_paged = True
                services.catalog.save_last_search_state(
                    query,
                    title_type,
                    results,
                    imdb_min=_format_rating_threshold(selected_imdb_min),
                    trakt_min=_format_rating_threshold(selected_trakt_min),
                )
                source_label = "Fresh Trakt search"
                if schedule_search_enrichment(
                    request.app,
                    results=results,
                    query=query,
                    title_type=title_type,
                    task_key=(
                        f"search_enrichment:{title_type or 'all'}:{query.casefold()}:{current_page}:"
                        f"{_format_rating_threshold(selected_imdb_min)}:{_format_rating_threshold(selected_trakt_min)}"
                    ),
                    save_search_state=False,
                ):
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
        try:
            history_state_keys = services.catalog.history_keys()
        except Exception:
            history_state_keys = set()
        release_tracking_keys: set[tuple[str, int]] = set()
        release_tracking_service = getattr(services, "release_tracking", None)
        if release_tracking_service is not None:
            try:
                release_tracking_keys = release_tracking_service.local_keys()
            except Exception:
                release_tracking_keys = set()
        if results:
            try:
                if not watchlist_keys_loaded:
                    watchlist_keys = await asyncio.to_thread(services.catalog.watchlist_keys, title_type=title_type)
                for item in results:
                    item.is_watchlisted = (item.title_type, int(item.trakt_id)) in watchlist_keys
                    release_at = _as_utc(item.released_at) if item.released_at is not None else None
                    item.is_future_release = release_at is not None and release_at > datetime.now(tz=UTC)
                    item.is_release_tracked = (item.title_type, int(item.trakt_id)) in release_tracking_keys
                    item.is_in_history = (item.title_type, int(item.trakt_id)) in history_state_keys
            except Exception:
                pass
        if provider_paged:
            paged_results = results
        else:
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
                "page_count": page_count,
                "query": query,
                "search_type": selected_type,
                "sort_mode": sort_mode or DEFAULT_SEARCH_SORT_MODE,
                "sort_modes": SEARCH_SORT_MODES,
                "results": paged_results,
                "search_history": services.catalog.search_history(),
                "source_label": source_label,
                "error_message": error_message,
                "page_kind": "search",
                "search_imdb_min": _format_rating_threshold(selected_imdb_min),
                "search_trakt_min": _format_rating_threshold(selected_trakt_min),
                "search_hide_watchlisted": selected_hide_watchlisted,
                "search_hide_history": selected_hide_history,
            },
        )

    @app.get("/explore", response_class=HTMLResponse)
    async def explore_page(
        request: Request,
        type: str = "show",
        feed: str = "anticipated",
        page: str = "1",
        imdb_min: str = "",
        trakt_min: str = "",
        catalog_shell: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        title_type = normalize_title_type(type) or "show"
        selected_feed = feed if feed in EXPLORE_FEEDS else "anticipated"
        _schedule_watchlist_snapshot_refresh(request, services)
        saved_filters = services.catalog.load_explore_rating_filters()
        effective_imdb_min = imdb_min if "imdb_min" in request.query_params else saved_filters.get("imdb_min", "")
        effective_trakt_min = trakt_min if "trakt_min" in request.query_params else saved_filters.get("trakt_min", "")
        remembered_imdb_min = _normalize_rating_threshold(effective_imdb_min)
        remembered_trakt_min = _normalize_rating_threshold(effective_trakt_min)
        remembered_hide_watchlisted = _saved_or_query_flag(
            request,
            "hide_watchlisted",
            bool(saved_filters.get("hide_watchlisted", False)),
        )
        remembered_hide_releases = _saved_or_query_flag(
            request,
            "hide_releases",
            bool(saved_filters.get("hide_releases", False)),
        )
        hide_releases = remembered_hide_releases if selected_feed == "anticipated" else False
        remembered_hide_history = _saved_or_query_flag(
            request,
            "hide_history",
            bool(saved_filters.get("hide_history", False)),
        )
        selected_imdb_min = remembered_imdb_min if selected_feed != "anticipated" else None
        selected_trakt_min = remembered_trakt_min if selected_feed != "anticipated" else None
        selected_hide_history = remembered_hide_history if selected_feed != "anticipated" else False
        if selected_feed != "anticipated":
            services.catalog.save_explore_rating_filters(
                _format_rating_threshold(selected_imdb_min),
                _format_rating_threshold(selected_trakt_min),
                hide_watchlisted=remembered_hide_watchlisted,
                hide_history=remembered_hide_history,
                hide_releases=remembered_hide_releases,
            )
        elif "hide_releases" in request.query_params:
            services.catalog.save_explore_rating_filters(
                str(saved_filters.get("imdb_min", "") or ""),
                str(saved_filters.get("trakt_min", "") or ""),
                hide_watchlisted=bool(saved_filters.get("hide_watchlisted", False)),
                hide_history=bool(saved_filters.get("hide_history", False)),
                hide_releases=hide_releases,
            )
        try:
            current_page = max(1, int(page))
        except (TypeError, ValueError):
            current_page = 1
        catalog_loading = str(catalog_shell or "").strip().lower() in {"1", "true", "yes", "on"}
        if catalog_loading:
            return render(
                request,
                "search_v2.html",
                {
                    "page_title": "Explore", "page_kind": "explore", "results": [], "error_message": "",
                    "page": current_page, "page_count": current_page, "has_next": False,
                    "explore_type": title_type, "explore_feed": selected_feed,
                    "explore_imdb_min": _format_rating_threshold(remembered_imdb_min),
                    "explore_trakt_min": _format_rating_threshold(remembered_trakt_min),
                    "explore_hide_watchlisted": remembered_hide_watchlisted,
                    "explore_hide_releases": remembered_hide_releases, "explore_hide_history": remembered_hide_history,
                    "catalog_loading": True,
                },
            )
        page_count = current_page
        results = []
        error_message = ""
        try:
            watchlist_keys = (
                await asyncio.to_thread(services.catalog.watchlist_keys, title_type=title_type)
                if selected_feed != "anticipated"
                else set()
            )
            release_tracking_service = getattr(services, "release_tracking", None)
            release_tracking_keys = (
                await asyncio.to_thread(release_tracking_service.local_keys)
                if selected_feed == "anticipated" and release_tracking_service is not None
                else set()
            )
            history_keys = await asyncio.to_thread(services.catalog.history_keys)
            excluded_keys = (
                set(release_tracking_keys)
                if selected_feed == "anticipated" and hide_releases
                else (set(watchlist_keys) if remembered_hide_watchlisted else set())
            )
            if selected_hide_history:
                excluded_keys.update(history_keys)
            max_scan_pages = max(1, int(getattr(services.auth.config, "explore_imdb_scan_page_limit", 10)))
            explore_args = {
                "page": current_page,
                "limit": SEARCH_PAGE_SIZE,
                "imdb_min": selected_imdb_min,
                "trakt_min": selected_trakt_min,
                "max_scan_pages": max_scan_pages,
                "excluded_keys": excluded_keys,
            }
            result_page = await asyncio.to_thread(
                services.catalog.local_explore_titles,
                title_type,
                selected_feed,
                **explore_args,
            )
            if result_page is None:
                result_page = await asyncio.to_thread(
                    services.catalog.refresh_explore_titles,
                    title_type,
                    selected_feed,
                    **explore_args,
                )
            else:
                request.app.state.bg_tasks.start(
                    (
                        f"catalog_explore_refresh:{title_type}:{selected_feed}:{current_page}:"
                        f"{_format_rating_threshold(selected_imdb_min)}:{_format_rating_threshold(selected_trakt_min)}"
                    ),
                    source="Explore refresh",
                    operations=services.operations,
                    fn=lambda: services.catalog.refresh_explore_titles(title_type, selected_feed, **explore_args),
                )
            results = result_page.items
            current_page = result_page.page
            page_count = result_page.page_count
            for item in results:
                item.is_watchlisted = (item.title_type, int(item.trakt_id)) in watchlist_keys
                item.is_release_tracked = (item.title_type, int(item.trakt_id)) in release_tracking_keys
                item.is_in_history = (item.title_type, int(item.trakt_id)) in history_keys
                if selected_feed == "anticipated":
                    item.release_distance_text = format_release_distance(item.released_at)
            _attach_search_rating_badges(services, results)
            schedule_search_enrichment(
                request.app,
                results=results,
                query="",
                title_type=title_type,
                task_key=(
                    f"explore_enrichment:{selected_feed}:{title_type}:{current_page}:"
                    f"{_format_rating_threshold(selected_imdb_min)}:{_format_rating_threshold(selected_trakt_min)}"
                ),
                source="Explore enrichment",
                save_search_state=False,
            )
        except Exception as exc:
            error_message = str(exc)
        return render(
            request,
            "search_v2.html",
            {
                "page_title": "Explore",
                "page_kind": "explore",
                "results": results,
                "error_message": error_message,
                "page": current_page,
                "page_count": page_count,
                "has_next": current_page < page_count,
                "explore_type": title_type,
                "explore_feed": selected_feed,
                "explore_imdb_min": _format_rating_threshold(remembered_imdb_min),
                "explore_trakt_min": _format_rating_threshold(remembered_trakt_min),
                "explore_hide_watchlisted": remembered_hide_watchlisted,
                "explore_hide_releases": remembered_hide_releases,
                "explore_hide_history": remembered_hide_history,
            },
        )

    @app.get("/watchlist", response_class=HTMLResponse)
    async def watchlist_page(
        request: Request,
        type: str = "all",
        release: str = "all",
        sort: str = "",
        direction: str = "desc",
        catalog_shell: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        results = []
        error_message = ""
        selected_type = normalize_title_type(type)
        release_filter = release if release in {"released", "upcoming"} else "all"
        sort_mode = normalize_watchlist_sort_mode(sort)
        sort_direction = "asc" if direction == "asc" else "desc"
        catalog_loading = str(catalog_shell or "").strip().lower() in {"1", "true", "yes", "on"}
        if catalog_loading:
            return render(
                request,
                "search_v2.html",
                {
                    "page_title": "Watchlist", "page_kind": "watchlist", "results": [], "error_message": "",
                    "page": 1, "has_next": False, "watchlist_type": selected_type or "all",
                    "watchlist_release": release_filter, "watchlist_sort_mode": sort_mode,
                    "watchlist_sort_modes": WATCHLIST_SORT_MODES, "watchlist_direction": sort_direction,
                    "catalog_loading": True,
                },
            )
        try:
            results = await asyncio.to_thread(services.catalog.local_watchlist_titles)
            request.app.state.bg_tasks.start(
                "watchlist_refresh",
                source="Watchlist refresh",
                operations=services.operations,
                fn=services.catalog.watchlist_titles,
            )
            if selected_type is not None:
                results = [item for item in results if item.title_type == selected_type]
            now = datetime.now(tz=UTC)
            if release_filter == "released":
                results = [
                    item for item in results
                    if item.released_at is not None and _known_utc(item.released_at) <= now
                ]
            elif release_filter == "upcoming":
                results = [
                    item for item in results
                    if item.released_at is not None and _known_utc(item.released_at) > now
                ]
            results = sort_watchlist_results(results, sort_mode, descending=sort_direction == "desc")
            _attach_search_rating_badges(services, results)
            schedule_search_enrichment(request.app, results=results, query="", title_type=None)
        except Exception as exc:
            error_message = str(exc)
        return render(
            request,
            "search_v2.html",
            {
                "page_title": "Watchlist",
                "page_kind": "watchlist",
                "results": results,
                "error_message": error_message,
                "page": 1,
                "has_next": False,
                "watchlist_type": selected_type or "all",
                "watchlist_release": release_filter,
                "watchlist_sort_mode": sort_mode,
                "watchlist_sort_modes": WATCHLIST_SORT_MODES,
                "watchlist_direction": sort_direction,
            },
        )

    @app.post("/watchlist/toggle")
    async def watchlist_toggle(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("title_type", "") or ""))
        try:
            trakt_id = int(payload.get("trakt_id") or 0)
        except (TypeError, ValueError):
            trakt_id = 0
        if title_type is None or trakt_id <= 0:
            return JSONResponse({"ok": False, "message": "Invalid watchlist item."}, status_code=400)
        watchlisted = bool(payload.get("watchlisted"))
        try:
            await asyncio.to_thread(
                services.catalog.set_watchlisted,
                title_type,
                trakt_id,
                watchlisted=watchlisted,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        action = "Added to" if watchlisted else "Removed from"
        services.operations.publish("Watchlist", f"{action} watchlist: {title_type} {trakt_id}")
        return JSONResponse({"ok": True, "watchlisted": watchlisted, "message": f"{action} watchlist."})

    @app.get("/release-tracking", response_class=HTMLResponse)
    async def release_tracking_page(request: Request) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        error_message = ""
        released = []
        upcoming = []
        try:
            items = await asyncio.to_thread(services.release_tracking.local_items)

            def refresh_releases() -> None:
                refreshed = services.release_tracking.refresh()
                services.release_tracking.refresh_anticipated_list_counts(refreshed)

            request.app.state.bg_tasks.start(
                "release_tracking_refresh",
                source="Release tracking refresh",
                operations=services.operations,
                fn=refresh_releases,
            )
            _attach_search_rating_badges(services, items)
            now = datetime.now(tz=UTC)
            released = sorted(
                [item for item in items if item.released_at is not None and _known_utc(item.released_at) <= now],
                key=lambda item: _as_utc(item.released_at) or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            upcoming = sorted(
                [item for item in items if item.released_at is None or _known_utc(item.released_at) > now],
                key=lambda item: (_as_utc(item.released_at) is None, _as_utc(item.released_at) or datetime.max.replace(tzinfo=UTC)),
            )
            schedule_search_enrichment(request.app, results=items, query="", title_type=None)
        except Exception as exc:
            error_message = str(exc)
        return render(
            request,
            "release_tracking.html",
            {
                "page_title": "Releases",
                "released": released,
                "upcoming": upcoming,
                "error_message": error_message,
            },
        )

    @app.post("/release-tracking/toggle")
    async def release_tracking_toggle(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("title_type", "") or ""))
        trakt_id = _optional_int(payload.get("trakt_id")) or 0
        if title_type is None or trakt_id <= 0:
            return JSONResponse({"ok": False, "message": "Invalid release tracking item."}, status_code=400)
        tracked = bool(payload.get("tracked"))
        list_count = _optional_int(payload.get("list_count"))
        try:
            await asyncio.to_thread(
                services.release_tracking.set_tracked,
                title_type,
                trakt_id,
                tracked=tracked,
                list_count=list_count,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({
            "ok": True,
            "tracked": tracked,
            "released_title_count": services.release_tracking.released_count(),
            "progress_waiting_title_count": services.release_tracking.progress_waiting_count(),
            "message": "Release tracking enabled." if tracked else "Release tracking disabled.",
        })

    @app.post("/release-tracking/acknowledge")
    async def release_tracking_acknowledge(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("title_type", "") or ""))
        trakt_id = _optional_int(payload.get("trakt_id")) or 0
        if title_type is None or trakt_id <= 0:
            return JSONResponse({"ok": False, "message": "Invalid release tracking item."}, status_code=400)
        try:
            acknowledged = await asyncio.to_thread(
                services.release_tracking.set_acknowledged,
                title_type,
                trakt_id,
                acknowledged=bool(payload.get("acknowledged")),
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({
            "ok": True,
            "acknowledged": acknowledged,
            "released_title_count": services.release_tracking.released_count(),
            "progress_waiting_title_count": services.release_tracking.progress_waiting_count(),
        })

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

    @app.get("/titles/{title_type}/{trakt_id}", response_class=RedirectResponse)
    async def title_details_page(title_type: str, trakt_id: int) -> RedirectResponse:
        normalized_type = normalize_title_type(title_type)
        if normalized_type is None:
            return RedirectResponse(url="/search", status_code=302)
        return RedirectResponse(
            url=f"https://trakt.tv/{normalized_type}s/{trakt_id}",
            status_code=302,
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
            normalized_provider = "trakt" if str(provider or "").strip().lower() == "trakt" else "imdb"
            if normalized_provider == "trakt" and (force_refresh or should_refresh_missing):
                refresh_keys = await asyncio.to_thread(
                    services.episode_ratings_matrix.select_trakt_rating_refresh_keys,
                    trakt_id,
                    force_refresh=force_refresh,
                )
                services.enrich_queue.submit_history_refresh(
                    viewport_tasks=[
                        build_history_episode_task(
                            title_key=f"matrix:{trakt_id}",
                            show_trakt_id=trakt_id,
                            season=season,
                            episode=episode,
                            priority=1,
                            trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
                            requested_parts=(ASSET_KIND_EPISODE_RATINGS,),
                        )
                        for season, episode in refresh_keys
                    ],
                    nearby_tasks=[],
                    page_tasks=[],
                )
            matrix = await asyncio.to_thread(
                services.episode_ratings_matrix.load_show_matrix,
                trakt_id,
                force_refresh=False,
                provider=normalized_provider,
                refresh_missing=False,
                allow_network_refresh=False,
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
    async def search_show_watch_panel(
        request: Request,
        trakt_id: int,
        season: int | None = None,
        refresh: bool = False,
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        try:
            if season is None:
                panel = await asyncio.to_thread(services.search_watch.load_show_panel, trakt_id)
            else:
                panel = await asyncio.to_thread(services.search_watch.load_show_panel, trakt_id, season)
            selected_season = _default_season_number(panel)
            if not getattr(panel, "seasons", []):
                services.enrich_queue.submit(
                    build_show_episode_hydration_task(title_key=f"watch-panel:{trakt_id}", trakt_id=trakt_id)
                )
            elif selected_season is not None:
                _enqueue_missing_season_stills(services, panel, selected_season)
            _enqueue_default_season_artwork(services, panel)
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
            if scope == "title" and getattr(services, "release_tracking", None) is not None:
                tracked_keys = await asyncio.to_thread(services.release_tracking.local_keys)
                if (title_type, trakt_id) in tracked_keys:
                    await asyncio.to_thread(services.release_tracking.set_tracked, title_type, trakt_id, tracked=False)
            removed_from_watchlist = False
            if scope == "title" and bool(payload.get("remove_from_watchlist")):
                await asyncio.to_thread(services.catalog.set_watchlisted, title_type, trakt_id, watchlisted=False)
                removed_from_watchlist = True
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        services.operations.publish("Search action", f"Marked watched from search: {title or title_type} ({count})")
        return JSONResponse(
            {
                "ok": True,
                "message": f"Marked {count} item{'s' if count != 1 else ''} watched.",
                "count": count,
                "removed_from_watchlist": removed_from_watchlist,
            }
        )

    @app.post("/search/unwatch")
    async def search_unwatch(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("title_type", "") or "")) or "show"
        scope = str(payload.get("scope", "") or "episode").strip().lower()
        try:
            trakt_id = int(payload.get("trakt_id") or 0)
        except (TypeError, ValueError):
            trakt_id = 0
        season = _optional_int(payload.get("season"))
        episode = _optional_int(payload.get("episode"))
        if trakt_id <= 0:
            return JSONResponse({"ok": False, "message": "Missing title identity."}, status_code=400)
        try:
            if scope == "episode":
                if title_type != "show" or season is None or season < 0 or episode is None or episode <= 0:
                    raise ValueError("Missing episode identity.")
                restore = await asyncio.to_thread(
                    services.search_watch.unmark_episode,
                    trakt_id=trakt_id,
                    season=season,
                    episode=episode,
                )
            elif scope in {"season", "title"}:
                if title_type == "show" and scope == "season" and (season is None or season < 0):
                    raise ValueError("Missing season identity.")
                restore = await asyncio.to_thread(
                    services.search_watch.unmark_scope,
                    title_type=title_type,
                    trakt_id=trakt_id,
                    scope=scope,
                    season=season,
                )
            else:
                raise ValueError("Unsupported watched-history scope.")
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

        bg_tasks = getattr(request.app.state, "bg_tasks", None)
        progress = getattr(services, "progress", None)
        if bg_tasks is not None and progress is not None and title_type == "show":
            bg_tasks.start(
                f"progress_unwatch_{trakt_id}",
                source="Progress refresh",
                operations=services.operations,
                fn=lambda: progress.refresh_show_progress(trakt_id, fresh=True),
            )
        if scope == "episode":
            detail = f"show {trakt_id} S{season:02d}E{episode:02d}"
        elif scope == "season":
            detail = f"show {trakt_id} S{season:02d}"
        else:
            detail = f"{title_type} {trakt_id}"
        services.operations.publish("Search action", f"Removed watched history: {detail}")
        return JSONResponse(
            {
                "ok": True,
                "message": "Watch removed.",
                "restore": restore,
                "still_watched": bool(restore.get("still_watched", False)) if scope != "episode" else True,
            }
        )

    @app.post("/search/restore-watch")
    async def search_restore_watch(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
            restore = dict(payload.get("restore") or {})
            if restore.get("kind") == "scope":
                items = []
                for raw_item in list(restore.get("items") or []):
                    item = dict(raw_item)
                    item["trakt_id"] = int(item.get("trakt_id") or 0)
                    item["watched_at"] = datetime.fromisoformat(str(item.get("watched_at") or ""))
                    item["watched_at_known"] = bool(item.get("watched_at_known", True))
                    if item["trakt_id"] <= 0:
                        raise ValueError("Missing title identity.")
                    items.append(item)
                await asyncio.to_thread(services.search_watch.restore_scope, items=items)
                restore_trakt_id = int(restore.get("trakt_id") or 0)
                restore_title_type = str(restore.get("title_type") or "")
            else:
                restore["trakt_id"] = int(restore.get("trakt_id") or 0)
                restore["season"] = int(restore.get("season") or 0)
                restore["episode"] = int(restore.get("episode") or 0)
                restore["watched_at"] = datetime.fromisoformat(str(restore.get("watched_at") or ""))
                restore["watched_at_known"] = bool(restore.get("watched_at_known", True))
                if restore["trakt_id"] <= 0 or restore["episode"] <= 0:
                    raise ValueError("Missing episode identity.")
                await asyncio.to_thread(services.search_watch.restore_episode, **restore)
                restore_trakt_id = int(restore["trakt_id"])
                restore_title_type = "show"
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        bg_tasks = getattr(request.app.state, "bg_tasks", None)
        progress = getattr(services, "progress", None)
        if bg_tasks is not None and progress is not None and restore_title_type == "show":
            trakt_id = restore_trakt_id
            bg_tasks.start(
                f"progress_restore_{trakt_id}",
                source="Progress refresh",
                operations=services.operations,
                fn=lambda: progress.refresh_show_progress(trakt_id, fresh=True),
            )
        return JSONResponse({"ok": True, "message": "Watch restored."})


def _optional_int(value) -> int | None:
    try:
        raw = str(value if value is not None else "").strip()
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _known_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _attach_search_rating_badges(services: ServiceContainer, results: list) -> None:
    try:
        badges = services.history.title_rating_badges([int(getattr(item, "trakt_id", 0) or 0) for item in results])
    except Exception:
        return
    for item in results:
        rating = badges.get(int(getattr(item, "trakt_id", 0) or 0))
        if rating is not None:
            item.title_episode_avg_rating = rating


def _default_season_number(panel) -> int | None:
    for season in getattr(panel, "seasons", []):
        if getattr(season, "is_default", False):
            return int(getattr(season, "season", 0) or 0)
    return None


def _enqueue_missing_season_stills(services: ServiceContainer, panel, selected_season: int) -> None:
    tasks = []
    for season in getattr(panel, "seasons", []):
        if int(getattr(season, "season", 0) or 0) != int(selected_season):
            continue
        for episode in getattr(season, "episodes", []):
            if not bool(getattr(episode, "is_released", False)) or str(getattr(episode, "still_url", "") or ""):
                continue
            tasks.append(
                build_history_episode_task(
                    title_key=f"watch-panel:{getattr(panel, 'trakt_id', 0)}",
                    show_trakt_id=int(getattr(panel, "trakt_id", 0) or 0),
                    season=int(getattr(episode, "season", 0) or 0),
                    episode=int(getattr(episode, "number", 0) or 0),
                    priority=1,
                    trigger=TRIGGER_PAGE_CONTEXT,
                    requested_parts=(ASSET_KIND_STILL,),
                )
            )
    if tasks:
        services.enrich_queue.submit_history_refresh(viewport_tasks=tasks, nearby_tasks=[], page_tasks=[])


def _enqueue_default_season_artwork(services: ServiceContainer, panel) -> None:
    urls = []
    for season in getattr(panel, "seasons", []):
        if not getattr(season, "is_default", False):
            continue
        for episode in getattr(season, "episodes", []):
            still_url = str(getattr(episode, "still_url", "") or "")
            if still_url:
                urls.append(tmdb_episode_preview_url(still_url))
        break
    if not urls:
        return
    image_queue = getattr(services, "image_queue", None)
    if image_queue is not None:
        image_queue.submit_many(urls, priority=1)


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
