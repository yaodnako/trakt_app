from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trakt_tracker.application.enrich_queue import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DROPPED,
    TASK_STATUS_FAILED,
    build_history_episode_task,
    build_history_title_task,
)
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_TITLE_RATINGS,
    TRIGGER_PAGE_CONTEXT,
    TRIGGER_VIEWPORT,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
)
from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import normalize_catalog_provider_mode, timezone_from_utc_offset
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.viewmodels import (
    EPISODE_RATINGS_READY_REFRESH_SECONDS,
    HISTORY_PAGE_SIZE,
    TITLE_RATINGS_READY_REFRESH_SECONDS,
    normalize_history_view,
    normalize_title_type,
    parse_bool_flag,
    ratings_refresh_due,
)

_HISTORY_TITLE_SORTS = {"rating", "last_watched", "release_year"}
_HISTORY_SORT_DIRECTIONS = {"asc", "desc"}


def register_history_routes(app, *, render, render_fragment) -> None:
    @app.get("/history", response_class=HTMLResponse)
    async def history_page(
        request: Request,
        type: str = "all",
        title: str = "",
        page: int = 1,
        rated_only: str = "0",
        flash: str = "",
        rate_trakt_id: int | None = None,
        rate_type: str = "",
        rate_season: str = "",
        rate_episode: str = "",
        rate_title: str = "",
        view: str = "episodes",
        sort: str = "last_watched",
        sort_dir: str = "desc",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        title_type = normalize_title_type(type)
        history_view = normalize_history_view(view)
        history_sort = _normalize_history_title_sort(sort)
        history_sort_direction = _normalize_history_sort_direction(sort_dir)
        current_page = max(1, page)
        title_filter = title.strip() or None
        only_rated_episodes = parse_bool_flag(rated_only)
        rows: list[dict] = []
        grouped_days: list[dict] = []
        title_cards: list[dict] = []
        if history_view == "titles":
            title_cards, has_next = _load_history_title_page_data(
                services,
                title_type=title_type,
                title_filter=title_filter,
                current_page=current_page,
                rated_only=only_rated_episodes,
                sort_by=history_sort,
                sort_direction=history_sort_direction,
            )
        else:
            rows, has_next, grouped_days = _load_history_page_data(
                services,
                title_type=title_type,
                title_filter=title_filter,
                current_page=current_page,
                rated_only=only_rated_episodes,
            )
        if _tmdb_history_enabled(services):
            title_options = services.tmdb_catalog.local_history_titles(title_type=title_type)
        else:
            title_options = services.history.history_titles(title_type=title_type)
        response = render(
            request,
            "history.html",
            {
                "page_title": "History",
                "history_rows": rows,
                "history_days": grouped_days,
                "history_title_cards": title_cards,
                "history_type": title_type or "all",
                "history_view": history_view,
                "history_sort": history_sort,
                "history_sort_direction": history_sort_direction,
                "history_title_filter": title.strip(),
                "history_rated_only": only_rated_episodes,
                "history_title_options": title_options,
                "page": current_page,
                "has_next": has_next,
                "rate_trakt_id": rate_trakt_id,
                "rate_type": normalize_title_type(rate_type) or "",
                "rate_season": _optional_int(rate_season),
                "rate_episode": _optional_int(rate_episode),
                "rate_title": rate_title,
                "history_sync_running": request.app.state.bg_tasks.is_running("history_sync"),
                "flash": flash,
            },
        )
        _schedule_title_alias_refresh(request, services)
        return response

    @app.get("/history/auto-sync")
    async def history_auto_sync(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        if _tmdb_history_enabled(services):
            return JSONResponse(
                {
                    "changed": False,
                    "started": False,
                    "message": "Remote sync is disabled in local mode.",
                }
            )
        bg_tasks = request.app.state.bg_tasks
        started = bg_tasks.start(
            "history_sync",
            source="History sync (auto)",
            operations=services.operations,
            fn=services.sync.maybe_refresh_history,
        )
        _schedule_title_alias_refresh(request, services)
        return JSONResponse(
            {
                "changed": False,
                "started": started,
                "message": "History auto-sync started." if started else "History auto-sync: already running or queued.",
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
        rated_only = parse_bool_flag(str(payload.get("rated_only", "0") or "0"))
        history_view = normalize_history_view(str(payload.get("view", "episodes") or "episodes"))
        history_sort = _normalize_history_title_sort(str(payload.get("sort", "last_watched") or "last_watched"))
        history_sort_direction = _normalize_history_sort_direction(str(payload.get("sort_dir", "desc") or "desc"))
        try:
            current_page = max(1, int(payload.get("page", 1) or 1))
        except (TypeError, ValueError):
            current_page = 1
        raw_visible_title_keys = payload.get("visible_title_keys", [])
        viewport_title_keys = _normalize_title_keys(payload.get("viewport_title_keys", []))
        nearby_title_keys = _normalize_title_keys(payload.get("nearby_title_keys", []))
        page_title_keys = _normalize_title_keys(payload.get("page_title_keys", []))
        force_visible_refresh = parse_bool_flag(str(payload.get("force_visible_refresh", "")))
        if not page_title_keys:
            page_title_keys = _normalize_title_keys(raw_visible_title_keys)
        try:
            queue_after_revision = max(0, int(payload.get("queue_after_revision", 0) or 0))
        except (TypeError, ValueError):
            queue_after_revision = 0
        if history_view == "titles":
            rows, has_next = _load_history_title_page_data(
                services,
                title_type=title_type,
                title_filter=title_filter,
                current_page=current_page,
                rated_only=rated_only,
                sort_by=history_sort,
                sort_direction=history_sort_direction,
            )
            current_title_groups = _title_card_map(rows)
            rows_by_title_key = {str(row["title_key"]): [row] for row in rows if row.get("title_key")}
        else:
            rows, has_next, grouped_days = _load_history_page_data(
                services,
                title_type=title_type,
                title_filter=title_filter,
                current_page=current_page,
                rated_only=rated_only,
            )
            current_title_groups = _title_group_map(grouped_days)
            rows_by_title_key = _rows_by_title_key(rows, services.auth.config.utc_offset)
        current_page_keys = list(current_title_groups.keys())
        stale_visible_title_keys = _select_stale_history_rating_title_keys(
            rows_by_title_key,
            viewport_title_keys or page_title_keys or current_page_keys,
        )
        if not _tmdb_history_enabled(services) and not request.app.state.bg_tasks.is_running("history_sync"):
            services.enrich_queue.submit_history_refresh(
                viewport_tasks=_build_history_bucket_tasks(
                    services,
                    rows_by_title_key,
                    viewport_title_keys,
                    priority=1,
                    trigger=TRIGGER_VIEWPORT,
                ),
                nearby_tasks=_build_history_bucket_tasks(
                    services,
                    rows_by_title_key,
                    nearby_title_keys,
                    priority=2,
                    trigger=TRIGGER_PAGE_CONTEXT,
                ),
                page_tasks=[
                    *_build_history_bucket_tasks(
                        services,
                        rows_by_title_key,
                        page_title_keys,
                        priority=3,
                        trigger=TRIGGER_PAGE_CONTEXT,
                    ),
                    *_build_history_bucket_tasks(
                        services,
                        rows_by_title_key,
                        stale_visible_title_keys if force_visible_refresh else [],
                        priority=1,
                        trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
                        title_requested_parts=(ASSET_KIND_TITLE_RATINGS,),
                        episode_requested_parts=(ASSET_KIND_EPISODE_RATINGS,),
                    ),
                ],
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
        fragment_template = "history_title_mode_card.html" if history_view == "titles" else "history_title_card.html"
        for title_key in affected_title_keys:
            title_group = current_title_groups.get(title_key)
            if title_group is None:
                continue
            rendered_groups.append(
                {
                    "title_key": title_key,
                    "html": render_fragment(
                        request,
                        fragment_template,
                        {
                            "title_group": title_group,
                            "title_card": title_group,
                            "history_type": title_type or "all",
                            "history_view": history_view,
                            "history_sort": history_sort,
                            "history_sort_direction": history_sort_direction,
                            "history_title_filter": title_filter_raw,
                            "history_rated_only": rated_only,
                            "page": current_page,
                        },
                    ),
                }
            )
        _schedule_title_alias_refresh(request, services)
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
        rated_only = parse_bool_flag(str(form.get("rated_only", "0") or "0"))
        history_view = normalize_history_view(str(form.get("view", "episodes") or "episodes"))
        history_sort = _normalize_history_title_sort(str(form.get("sort", "last_watched") or "last_watched"))
        history_sort_direction = _normalize_history_sort_direction(str(form.get("sort_dir", "desc") or "desc"))
        try:
            page = max(1, int(str(form.get("page", "1") or "1")))
        except ValueError:
            page = 1
        started = bg_tasks.start(
            "history_sync",
            source="History sync (manual full)",
            operations=services.operations,
            fn=services.sync.sync_assets_full,
        )
        flash = "History sync started." if started else "History sync is already running."
        redirect_url = (
            f"/history?type={history_type}&title={quote(title_filter)}&page={page}"
            f"&rated_only={'1' if rated_only else '0'}&view={history_view}"
            f"&sort={history_sort}&sort_dir={history_sort_direction}&flash={quote(flash)}"
        )
        return RedirectResponse(url=redirect_url, status_code=303)

    @app.get("/history/sync-status")
    async def history_sync_status(request: Request, after: int = 0) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        raw_events = [
            event
            for event in services.operations.list_after(after)
            if str(event.get("source", "")).startswith("History sync")
        ]
        latest_progress = ""
        recent_events: list[dict] = []
        seen_messages: set[str] = set()
        for event in raw_events:
            message = str(event.get("message", "") or "")
            if "%" in message:
                latest_progress = message
                continue
            if message in seen_messages:
                continue
            seen_messages.add(message)
            recent_events.append(event)
        return JSONResponse(
            {
                "running": bg_tasks.is_running("history_sync"),
                "progress_message": latest_progress,
                "events": recent_events[-6:],
            }
        )

    @app.post("/history/rate")
    async def history_rate(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        history_type = normalize_title_type(str(form.get("type", "all") or "all")) or "all"
        title_filter = str(form.get("title_filter", "") or "")
        rated_only = parse_bool_flag(str(form.get("rated_only", "0") or "0"))
        history_view = normalize_history_view(str(form.get("view", "episodes") or "episodes"))
        history_sort = _normalize_history_title_sort(str(form.get("sort", "last_watched") or "last_watched"))
        history_sort_direction = _normalize_history_sort_direction(str(form.get("sort_dir", "desc") or "desc"))
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
        redirect_url = (
            f"/history?type={history_type}&title={quote(title_filter)}&page={page}"
            f"&rated_only={'1' if rated_only else '0'}&view={history_view}"
            f"&sort={history_sort}&sort_dir={history_sort_direction}&flash={quote(flash)}"
        )
        return RedirectResponse(url=redirect_url, status_code=303)


def _load_history_page_data(
    services: ServiceContainer,
    *,
    title_type: str | None,
    title_filter: str | None,
    current_page: int,
    rated_only: bool,
) -> tuple[list[dict], bool, list[dict]]:
    if _tmdb_history_enabled(services):
        rows = services.tmdb_catalog.local_history_rows(
            title_type=title_type,
            title_filter=title_filter,
            rated_only=rated_only,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=(current_page - 1) * HISTORY_PAGE_SIZE,
        )
    else:
        rows = services.history.history(
            title_type=title_type,
            title_filter=title_filter,
            rated_only=rated_only,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=(current_page - 1) * HISTORY_PAGE_SIZE,
        )
    has_next = len(rows) > HISTORY_PAGE_SIZE
    rows = rows[:HISTORY_PAGE_SIZE]
    grouped_days = _group_history_rows(rows, services.auth.config.utc_offset)
    return rows, has_next, grouped_days


def _load_history_title_page_data(
    services: ServiceContainer,
    *,
    title_type: str | None,
    title_filter: str | None,
    current_page: int,
    rated_only: bool,
    sort_by: str,
    sort_direction: str,
) -> tuple[list[dict], bool]:
    if _tmdb_history_enabled(services):
        rows = services.tmdb_catalog.local_history_title_summaries(
            title_type=title_type,
            title_filter=title_filter,
            rated_only=rated_only,
            sort_by=sort_by,
            sort_direction=sort_direction,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=(current_page - 1) * HISTORY_PAGE_SIZE,
        )
    else:
        rows = services.history.history_title_summaries(
            title_type=title_type,
            title_filter=title_filter,
            rated_only=rated_only,
            sort_by=sort_by,
            sort_direction=sort_direction,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=(current_page - 1) * HISTORY_PAGE_SIZE,
        )
    has_next = len(rows) > HISTORY_PAGE_SIZE
    return rows[:HISTORY_PAGE_SIZE], has_next


def _normalize_history_title_sort(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _HISTORY_TITLE_SORTS else "last_watched"


def _normalize_history_sort_direction(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _HISTORY_SORT_DIRECTIONS else "desc"


def _tmdb_history_enabled(services: ServiceContainer) -> bool:
    tmdb_catalog = getattr(services, "tmdb_catalog", None)
    return bool(
        normalize_catalog_provider_mode(
            getattr(services.auth.config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview"
        and callable(getattr(tmdb_catalog, "local_history_rows", None))
        and callable(getattr(tmdb_catalog, "local_history_title_summaries", None))
    )


def _sort_combined_history_titles(
    rows: list[dict],
    *,
    sort_by: str,
    sort_direction: str,
) -> list[dict]:
    def value(row):
        if sort_by == "rating":
            return row.get("my_rating")
        if sort_by == "release_year":
            return row.get("title_year")
        watched_at = row.get("last_watched_at")
        if not row.get("last_watched_at_known", True) or not isinstance(watched_at, datetime):
            return None
        known = watched_at.replace(tzinfo=UTC) if watched_at.tzinfo is None else watched_at.astimezone(UTC)
        return known.timestamp()

    known = [row for row in rows if value(row) is not None]
    unknown = [row for row in rows if value(row) is None]
    known.sort(key=value, reverse=sort_direction != "asc")
    unknown.sort(
        key=lambda row: (
            str(row.get("title", "")).casefold(),
            int(row.get("tmdb_id") or row.get("title_trakt_id") or 0),
        )
    )
    return [*known, *unknown]


def _normalize_title_keys(raw_keys) -> list[str]:
    return [
        str(key)
        for key in dict.fromkeys(raw_keys if isinstance(raw_keys, list) else [])
        if isinstance(key, str) and key.strip()
    ]


def _optional_int(value) -> int | None:
    try:
        raw = str(value if value is not None else "").strip()
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


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
    trigger: str = TRIGGER_VIEWPORT,
    title_requested_parts=(),
    episode_requested_parts=(),
) -> list:
    tasks: list = []
    for title_key in title_keys:
        title_rows = rows_by_title_key.get(title_key, [])
        if not title_rows:
            continue
        enrich_rows = [row for row in title_rows if row.get("provider", "trakt") != "tmdb"]
        if not enrich_rows:
            continue
        title_enrich_keys = services.catalog.select_title_enrich_keys(
            enrich_rows,
            trigger=trigger,
            requested_parts=title_requested_parts,
        )
        for trakt_id, title_type in title_enrich_keys:
            tasks.append(
                build_history_title_task(
                    title_key=title_key,
                    trakt_id=trakt_id,
                    title_type=title_type,
                    priority=priority,
                    trigger=trigger,
                    requested_parts=title_requested_parts,
                )
            )
        episode_enrich_keys = services.history.select_episode_enrich_keys(
            enrich_rows,
            trigger=trigger,
            requested_parts=episode_requested_parts,
        )
        for show_trakt_id, season, episode in episode_enrich_keys:
            tasks.append(
                build_history_episode_task(
                    title_key=title_key,
                    show_trakt_id=show_trakt_id,
                    season=season,
                    episode=episode,
                    priority=priority,
                    trigger=trigger,
                    requested_parts=episode_requested_parts,
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


def _title_card_map(title_cards: list[dict]) -> OrderedDict[str, dict]:
    result: OrderedDict[str, dict] = OrderedDict()
    for title_card in title_cards:
        title_key = str(title_card.get("title_key", "") or "")
        if title_key:
            result[title_key] = title_card
    return result


def _group_history_rows(rows: list[dict], utc_offset: str) -> list[dict]:
    tz = timezone_from_utc_offset(utc_offset)
    groups: OrderedDict[str, dict] = OrderedDict()
    for row in rows:
        watched_at = row.get("watched_at")
        if not row.get("watched_at_known", True):
            day_label = "Без даты"
        elif watched_at is None:
            day_label = "Unknown date"
        else:
            normalized = watched_at if watched_at.tzinfo is not None else watched_at.replace(tzinfo=UTC)
            local_dt = normalized.astimezone(tz)
            day_label = local_dt.strftime("%d.%m.%Y")
        group = groups.setdefault(day_label, {"day_label": day_label, "count": 0, "title_groups": [], "_title_map": OrderedDict()})
        group["count"] += 1
        title_key = (row.get("type"), *_history_identity(row))
        title_group = group["_title_map"].get(title_key)
        if title_group is None:
            title_group = {
                "title_key": _history_title_key_for_day(day_label, row),
                "provider": row.get("provider", "trakt"),
                "tmdb_id": row.get("tmdb_id"),
                "title_trakt_id": row.get("title_trakt_id"),
                "title": row.get("title", ""),
                "title_slug": row.get("title_slug", ""),
                "type": row.get("type", ""),
                "poster_url": row.get("poster_url", ""),
                "title_poster_status": row.get("title_poster_status", "unknown"),
                "title_trakt_rating": row.get("title_trakt_rating"),
                "title_trakt_votes": row.get("title_trakt_votes"),
                "title_tmdb_rating": row.get("title_tmdb_rating"),
                "title_tmdb_votes": row.get("title_tmdb_votes"),
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
        for title_group in day_group.get("title_groups", []):
            title_group["entries"] = list(reversed(title_group.get("entries", [])))
        day_group.pop("_title_map", None)
    return list(groups.values())


def _history_identity(row: dict) -> tuple[str, int]:
    if row.get("provider") == "tmdb" and int(row.get("tmdb_id") or 0) > 0:
        return "tmdb", int(row["tmdb_id"])
    return "trakt", int(row.get("title_trakt_id") or 0)


def _history_title_key_for_day(day_label: str, row: dict) -> str:
    provider, provider_id = _history_identity(row)
    if provider == "tmdb":
        return f"{day_label}:{row.get('type', '')}:tmdb:{provider_id}"
    return f"{day_label}:{row.get('type', '')}:{provider_id}"


def _history_title_key_for_row(row: dict, utc_offset: str) -> str:
    watched_at = row.get("watched_at")
    if not row.get("watched_at_known", True):
        day_label = "Без даты"
    elif watched_at is None:
        day_label = "Unknown date"
    else:
        tz = timezone_from_utc_offset(utc_offset)
        normalized = watched_at if watched_at.tzinfo is not None else watched_at.replace(tzinfo=UTC)
        day_label = normalized.astimezone(tz).strftime("%d.%m.%Y")
    return _history_title_key_for_day(day_label, row)


def _select_stale_history_rating_title_keys(rows_by_title_key: dict[str, list[dict]], title_keys: list[str]) -> list[str]:
    result: list[str] = []
    for title_key in title_keys:
        rows = rows_by_title_key.get(title_key, [])
        if not rows:
            continue
        title_due = any(
            ratings_refresh_due(
                row.get("title_ratings_status"),
                row.get("title_ratings_refreshed_at"),
                asset_kind=ASSET_KIND_TITLE_RATINGS,
                ready_ttl_seconds=TITLE_RATINGS_READY_REFRESH_SECONDS,
            )
            for row in rows
        )
        if title_due:
            result.append(title_key)
            continue
        episode_due = any(
            row.get("type") == "show"
            and row.get("season") is not None
            and row.get("episode") is not None
            and ratings_refresh_due(
                row.get("episode_trakt_status"),
                row.get("episode_trakt_refreshed_at"),
                asset_kind=ASSET_KIND_EPISODE_RATINGS,
                ready_ttl_seconds=EPISODE_RATINGS_READY_REFRESH_SECONDS,
            )
            for row in rows
        )
        if episode_due:
            result.append(title_key)
    return list(dict.fromkeys(result))


def _schedule_title_alias_refresh(request: Request, services: ServiceContainer) -> None:
    if _tmdb_history_enabled(services):
        return
    alias_service = getattr(services, "title_aliases", None)
    if alias_service is None:
        return
    bg_tasks = request.app.state.bg_tasks
    task_key = "title_aliases_ru"
    if bg_tasks.is_running(task_key) or not alias_service.has_due_history_titles(language="ru"):
        return
    bg_tasks.start(
        task_key,
        source="Russian title aliases",
        operations=services.operations,
        fn=lambda: alias_service.refresh_due_history_titles(language="ru"),
    )
