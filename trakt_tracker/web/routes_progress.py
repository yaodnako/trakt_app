from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trakt_tracker.application.enrich_queue import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DROPPED,
    TASK_STATUS_FAILED,
    build_progress_episode_task,
    build_progress_title_task,
)
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_TITLE_RATINGS,
    TRIGGER_PAGE_CONTEXT,
    TRIGGER_VIEWPORT,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
)
from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import ConfigStore, normalize_catalog_provider_mode
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.watch_follow_up import schedule_watch_follow_up
from trakt_tracker.web.viewmodels import (
    DEFAULT_PROGRESS_SORT_DIRECTION,
    DEFAULT_PROGRESS_SORT_MODE,
    filter_progress_items,
    normalize_progress_sort_direction,
    normalize_progress_sort_mode,
    parse_bool_flag,
    progress_has_released_next_episode,
    PROGRESS_SORT_OPTIONS,
    ratings_refresh_due,
    TITLE_RATINGS_READY_REFRESH_SECONDS,
    EPISODE_RATINGS_READY_REFRESH_SECONDS,
)

_PROGRESS_PAGE_LIMIT = 50


@dataclass(frozen=True, slots=True)
class _ProgressPageState:
    hide_upcoming: bool
    show_paused: bool
    show_dropped: bool
    sort_mode: str
    sort_direction: str

    @property
    def view(self) -> str:
        if self.show_dropped:
            return "dropped"
        if self.show_paused:
            return "paused"
        return "active"

    @property
    def descending(self) -> bool:
        return self.sort_direction == "desc"

    def context(self) -> dict[str, object]:
        return {
            "hide_upcoming": self.hide_upcoming,
            "show_paused": self.show_paused,
            "show_dropped": self.show_dropped,
            "sort_mode": self.sort_mode,
            "sort_direction": self.sort_direction,
        }


def register_progress_routes(app, *, render, progress_redirect) -> None:
    @app.get("/progress", response_class=HTMLResponse)
    async def progress_page(
        request: Request,
        hide_upcoming: str = "",
        show_paused: str = "",
        show_dropped: str = "",
        sort: str = "",
        direction: str = "",
        flash: str = "",
        rate_provider: str = "trakt",
        rate_trakt_id: int | None = None,
        rate_tmdb_id: int | None = None,
        rate_season: int | None = None,
        rate_episode: int | None = None,
        rate_title: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        config = services.auth.config
        state = _parse_progress_query_state(
            config,
            hide_upcoming=hide_upcoming,
            show_paused=show_paused,
            show_dropped=show_dropped,
            sort_mode=sort,
            sort_direction=direction,
        )
        config_changed = False
        if config.hide_upcoming_in_progress != state.hide_upcoming:
            config.hide_upcoming_in_progress = state.hide_upcoming
            config_changed = True
        if getattr(config, "show_paused_in_progress", False) != state.show_paused:
            config.show_paused_in_progress = state.show_paused
            config_changed = True
        if config.show_dropped_in_progress != state.show_dropped:
            config.show_dropped_in_progress = state.show_dropped
            config_changed = True
        if getattr(config, "web_progress_sort_mode", DEFAULT_PROGRESS_SORT_MODE) != state.sort_mode:
            config.web_progress_sort_mode = state.sort_mode
            config_changed = True
        if getattr(config, "web_progress_sort_direction", DEFAULT_PROGRESS_SORT_DIRECTION) != state.sort_direction:
            config.web_progress_sort_direction = state.sort_direction
            config_changed = True
        if config_changed:
            ConfigStore().save(config)
        _all_items, new_items, progress_items = _load_progress_items(services, state=state)
        unseen_episode_ids = services.notifications.unseen_episode_ids()
        return render(
            request,
            "progress.html",
            {
                "page_title": "Up next",
                "new_items": new_items,
                "progress_items": progress_items,
                "unseen_episode_ids": unseen_episode_ids,
                **state.context(),
                "progress_sort_options": PROGRESS_SORT_OPTIONS,
                "progress_sync_running": request.app.state.bg_tasks.is_running("progress_sync"),
                "tmdb_preview": _tmdb_preview_mode(services),
                "flash": flash,
                "rate_provider": rate_provider,
                "rate_trakt_id": rate_trakt_id,
                "rate_tmdb_id": rate_tmdb_id,
                "rate_season": rate_season,
                "rate_episode": rate_episode,
                "rate_title": rate_title,
            },
        )

    @app.post("/progress/refresh")
    async def progress_refresh(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        state = _parse_progress_query_state(
            services.auth.config,
            hide_upcoming=str(payload.get("hide_upcoming", "")),
            show_paused=str(payload.get("show_paused", "")),
            show_dropped=str(payload.get("show_dropped", "")),
            sort_mode=str(payload.get("sort", "")),
            sort_direction=str(payload.get("direction", "")),
        )
        viewport_card_keys = _normalize_card_keys(payload.get("viewport_card_keys", []))
        nearby_card_keys = _normalize_card_keys(payload.get("nearby_card_keys", []))
        page_card_keys = _normalize_card_keys(payload.get("page_card_keys", []))
        force_visible_refresh = parse_bool_flag(str(payload.get("force_visible_refresh", "")))
        try:
            queue_after_revision = max(0, int(payload.get("queue_after_revision", 0) or 0))
        except (TypeError, ValueError):
            queue_after_revision = 0
        all_items, new_items, progress_items = _load_progress_items(
            services,
            state=state,
        )
        items_by_key = {_progress_card_key(item): item for item in all_items}
        current_page_keys = [_progress_card_key(item) for item in all_items]
        stale_visible_card_keys = _select_stale_progress_rating_card_keys(
            items_by_key,
            viewport_card_keys or page_card_keys or current_page_keys,
        )
        if not _tmdb_preview_mode(services) and not request.app.state.bg_tasks.is_running("progress_sync"):
            services.enrich_queue.submit_progress_refresh(
                viewport_tasks=_build_progress_bucket_tasks(
                    services,
                    items_by_key,
                    viewport_card_keys,
                    priority=1,
                    trigger=TRIGGER_VIEWPORT,
                ),
                nearby_tasks=_build_progress_bucket_tasks(
                    services,
                    items_by_key,
                    nearby_card_keys,
                    priority=2,
                    trigger=TRIGGER_PAGE_CONTEXT,
                ),
                page_tasks=[
                    *_build_progress_bucket_tasks(
                        services,
                        items_by_key,
                        page_card_keys,
                        priority=3,
                        trigger=TRIGGER_PAGE_CONTEXT,
                    ),
                    *_build_progress_bucket_tasks(
                        services,
                        items_by_key,
                        stale_visible_card_keys if force_visible_refresh else [],
                        priority=1,
                        trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
                        title_requested_parts=(ASSET_KIND_TITLE_RATINGS,),
                        episode_requested_parts=(ASSET_KIND_EPISODE_RATINGS,),
                    ),
                ],
            )
        relevant_card_keys = set(page_card_keys or current_page_keys)
        queue = services.enrich_queue.list_updates(
            after_revision=queue_after_revision,
            relevant_title_keys=relevant_card_keys,
        )
        missing_card_keys = [key for key in page_card_keys if key not in items_by_key]
        affected_card_keys = []
        for update in queue.get("updates", []):
            if update.get("status") not in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_DROPPED}:
                continue
            for card_key in update.get("affected_title_keys", []):
                if card_key in items_by_key and card_key not in affected_card_keys:
                    affected_card_keys.append(card_key)
        rendered_cards = []
        for card_key in affected_card_keys:
            item = items_by_key.get(card_key)
            if item is None:
                continue
            rendered_cards.append(
                {
                    "card_key": card_key,
                    "html": request.app.state.render_fragment(
                        request,
                        "progress_card.html",
                        {
                            "progress_item": item,
                            "progress_is_new": item in new_items,
                            "tmdb_preview": _tmdb_preview_mode(services),
                            **state.context(),
                        },
                    ),
                }
            )
        return JSONResponse(
            {
                "cards": rendered_cards,
                "missing_card_keys": missing_card_keys,
                "progress_sync_running": request.app.state.bg_tasks.is_running("progress_sync"),
                "queue": queue,
                "page_changed": bool(page_card_keys) and page_card_keys != current_page_keys,
                "sections_html": (
                    request.app.state.render_fragment(
                        request,
                        "progress_sections.html",
                        {
                            "new_items": new_items,
                            "progress_items": progress_items,
                            "tmdb_preview": _tmdb_preview_mode(services),
                            **state.context(),
                        },
                    )
                    if bool(page_card_keys) and page_card_keys != current_page_keys
                    else ""
                ),
            }
        )

    @app.post("/progress/sync")
    async def progress_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        form = await request.form()
        state = _parse_progress_form_state(form)
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="Remote sync is disabled in local mode.")
        started = bg_tasks.start(
            "progress_sync",
            source="Progress sync (manual full)",
            operations=services.operations,
            fn=lambda: services.progress.sync_progress(
                view=state.view,
                sort_mode=state.sort_mode,
                descending=state.descending,
                force_refresh=True,
                force_full_assets=False,
            ),
        )
        return progress_redirect(
            **state.context(),
            flash="Progress sync started." if started else "Progress sync is already running.",
        )

    @app.get("/progress/tmdb/{tmdb_id}/play")
    async def tmdb_progress_play(
        request: Request,
        tmdb_id: int,
        hide_upcoming: str = "",
        show_paused: str = "",
        show_dropped: str = "",
        sort: str = "",
        direction: str = "",
    ) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        state = _parse_progress_query_state(
            services.auth.config,
            hide_upcoming=hide_upcoming,
            show_paused=show_paused,
            show_dropped=show_dropped,
            sort_mode=sort,
            sort_direction=direction,
        )
        current = _find_tmdb_progress_item(services, tmdb_id, state=state)
        if current is None:
            return progress_redirect(**state.context(), flash="Title not found.")
        target_url = services.play.resolve_kinopoisk_url(current.title)
        if not target_url:
            return progress_redirect(
                **state.context(),
                flash=f"Kinopoisk not found for {current.title}.",
            )
        return RedirectResponse(url=target_url, status_code=302)

    @app.post("/progress/tmdb/{tmdb_id}/watch")
    async def tmdb_progress_watch(request: Request, tmdb_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        current = _find_tmdb_progress_item(services, tmdb_id, state=state)
        if current is None or current.next_episode is None:
            return progress_redirect(
                **state.context(),
                flash="No next episode to mark watched.",
            )
        episode = current.next_episode
        try:
            item = await asyncio.to_thread(services.tmdb_catalog.get_item, "show", tmdb_id)
            await asyncio.to_thread(
                services.tmdb_catalog.mark_watched,
                item,
                watched_at=datetime.now(tz=UTC),
                season=episode.season,
                episode=episode.number,
            )
            mark_episode_seen = getattr(services.notifications, "mark_episode_seen", None)
            if callable(mark_episode_seen):
                mark_episode_seen(
                    show_trakt_id=-int(tmdb_id),
                    show_title=current.title,
                    episode=episode,
                )
        except Exception as exc:
            services.operations.publish("Progress action", f"Local watch failed: {exc}")
            return progress_redirect(**state.context(), flash=f"Watch failed: {exc}")
        return progress_redirect(
            **state.context(),
            flash=f"Marked {current.title} {episode.season:02d}x{episode.number:02d} watched.",
            rate_provider="tmdb",
            rate_tmdb_id=tmdb_id,
            rate_season=episode.season,
            rate_episode=episode.number,
            rate_title=current.title,
        )

    @app.post("/progress/tmdb/{tmdb_id}/seen")
    async def tmdb_progress_seen(request: Request, tmdb_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        current = _find_tmdb_progress_item(services, tmdb_id, state=state)
        if current is None or current.next_episode is None:
            return progress_redirect(
                **state.context(),
                flash="No next episode to mark seen.",
            )
        try:
            mark_episode_seen = getattr(services.notifications, "mark_episode_seen", None)
            if callable(mark_episode_seen):
                mark_episode_seen(
                    show_trakt_id=-int(tmdb_id),
                    show_title=current.title,
                    episode=current.next_episode,
                )
        except Exception as exc:
            return progress_redirect(**state.context(), flash=f"Notification update failed: {exc}")
        return progress_redirect(**state.context(), flash="Notification marked seen.")

    @app.post("/progress/tmdb/{tmdb_id}/drop-toggle")
    async def tmdb_progress_drop_toggle(request: Request, tmdb_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        current_is_dropped = parse_bool_flag(str(form.get("is_dropped", "")))
        try:
            await asyncio.to_thread(
                services.tmdb_catalog.set_progress_dropped,
                tmdb_id,
                dropped=not current_is_dropped,
            )
        except Exception as exc:
            return progress_redirect(**state.context(), flash=f"Drop failed: {exc}")
        return progress_redirect(
            **state.context(),
            flash="Show restored." if current_is_dropped else "Show dropped.",
        )

    @app.post("/progress/tmdb/{tmdb_id}/pause-toggle")
    async def tmdb_progress_pause_toggle(request: Request, tmdb_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        current = _find_tmdb_progress_item(services, tmdb_id, state=state)
        if current is None:
            return progress_redirect(**state.context(), flash="Title not found.")
        current_is_paused = bool(getattr(current, "is_paused", False))
        try:
            await asyncio.to_thread(
                services.tmdb_catalog.set_progress_paused,
                tmdb_id,
                paused=not current_is_paused,
            )
        except Exception as exc:
            return progress_redirect(**state.context(), flash=f"Pause failed: {exc}")
        return progress_redirect(
            **state.context(),
            flash="Show resumed." if current_is_paused else "Show paused.",
        )

    @app.get("/progress/{trakt_id}/play")
    async def progress_play(
        request: Request,
        trakt_id: int,
        hide_upcoming: str = "",
        show_paused: str = "",
        show_dropped: str = "",
        sort: str = "",
        direction: str = "",
    ) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        state = _parse_progress_query_state(
            services.auth.config,
            hide_upcoming=hide_upcoming,
            show_paused=show_paused,
            show_dropped=show_dropped,
            sort_mode=sort,
            sort_direction=direction,
        )
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="TMDb mode uses local progress actions.")
        current = _find_progress_item(services, trakt_id, state=state)
        services.operations.publish("Play", f"Play requested: trakt_id={trakt_id}")
        if current is None:
            return progress_redirect(
                **state.context(),
                flash="Title not found.",
            )
        target_url = services.play.resolve_kinopoisk_url(current.title)
        if not target_url:
            return progress_redirect(
                **state.context(),
                flash=f"Kinopoisk not found for {current.title}.",
            )
        return RedirectResponse(url=target_url, status_code=302)

    @app.post("/progress/{trakt_id}/watch")
    async def progress_watch(request: Request, trakt_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="TMDb mode uses local progress actions.")
        current = _find_progress_item(services, trakt_id, state=state)
        if current is None or current.next_episode is None:
            return progress_redirect(
                **state.context(),
                flash="No next episode to mark watched.",
            )
        episode = current.next_episode
        services.operations.publish("Progress action", f"Mark watched: {current.title} S{episode.season:02d}E{episode.number:02d}")
        try:
            await asyncio.to_thread(
                services.interactions.mark_progress_episode_watched,
                current,
                watched_at=datetime.now(),
            )
        except Exception as exc:
            services.operations.publish("Progress action", f"Mark watched failed: {exc}")
            return progress_redirect(
                **state.context(),
                flash=f"Watch failed: {exc}",
            )
        try:
            schedule_watch_follow_up(
                request.app,
                title_type="show",
                trakt_id=current.trakt_id,
                remove_from_release_tracking=True,
            )
        except Exception as exc:
            services.operations.publish("Progress warning", f"Watch follow-up scheduling failed: {exc}")
        return progress_redirect(
            **state.context(),
            flash=f"Marked {current.title} {episode.season:02d}x{episode.number:02d} watched.",
            rate_trakt_id=current.trakt_id,
            rate_season=episode.season,
            rate_episode=episode.number,
            rate_title=current.title,
        )

    @app.post("/progress/{trakt_id}/seen")
    async def progress_seen(request: Request, trakt_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="TMDb mode uses local progress actions.")
        current = _find_progress_item(services, trakt_id, state=state)
        if current is None or current.next_episode is None:
            return progress_redirect(
                **state.context(),
                flash="No released episode to mark seen.",
            )
        episode = current.next_episode
        try:
            services.interactions.mark_progress_episode_seen(current, now=datetime.now(tz=UTC))
        except RuntimeError as exc:
            return progress_redirect(
                **state.context(),
                flash=str(exc),
            )
        services.operations.publish("Progress action", f"Marked seen: {current.title} S{episode.season:02d}E{episode.number:02d}")
        return progress_redirect(
            **state.context(),
            flash=f"Marked {current.title} {episode.season:02d}x{episode.number:02d} seen.",
        )

    @app.post("/progress/rate")
    async def progress_rate(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="TMDb mode uses local ratings.")
        trakt_id = int(str(form.get("trakt_id", "0") or "0"))
        season = int(str(form.get("season", "0") or "0"))
        episode = int(str(form.get("episode", "0") or "0"))
        title = str(form.get("title", "") or "")
        skip_rating = parse_bool_flag(str(form.get("skip_rating", "")))
        if not skip_rating:
            rating = int(str(form.get("rating", "0") or "0"))
            services.operations.publish("Progress action", f"Save rating: {title} S{season:02d}E{episode:02d} -> {rating}/10")
            try:
                services.interactions.save_rating(
                    RatingInput(
                        title_type="show",
                        trakt_id=trakt_id,
                        rating=rating,
                        season=season,
                        episode=episode,
                    ),
                    title=title,
                )
                flash = f"Saved rating for {title}."
            except Exception as exc:
                flash = f"Rating failed: {exc}"
        else:
            flash = "Skipped rating."
        if getattr(services, "trakt_sync", None) is None:
            services.progress.sync_progress(
                [trakt_id],
                view=state.view,
                sort_mode=state.sort_mode,
                descending=state.descending,
            )
        return progress_redirect(
            **state.context(),
            flash=flash,
        )

    @app.post("/progress/{trakt_id}/drop-toggle")
    async def progress_drop_toggle(request: Request, trakt_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="TMDb mode uses local progress actions.")
        current_is_dropped = parse_bool_flag(str(form.get("is_dropped", "")))
        if current_is_dropped:
            services.interactions.set_progress_dropped(trakt_id, dropped=False)
            flash = "Show restored."
        else:
            services.interactions.set_progress_dropped(trakt_id, dropped=True)
            flash = "Show dropped."
        return progress_redirect(
            **state.context(),
            flash=flash,
        )

    @app.post("/progress/{trakt_id}/pause-toggle")
    async def progress_pause_toggle(request: Request, trakt_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        state = _parse_progress_form_state(form)
        if _tmdb_preview_mode(services):
            return progress_redirect(**state.context(), flash="TMDb mode uses local progress actions.")
        current = _find_progress_item(services, trakt_id, state=state)
        if current is None:
            return progress_redirect(
                **state.context(),
                flash="Title not found.",
            )
        current_is_paused = bool(getattr(current, "is_paused", False))
        services.interactions.set_progress_paused(
            trakt_id,
            paused=not current_is_paused,
            progress=current,
        )
        return progress_redirect(
            **state.context(),
            flash="Show resumed." if current_is_paused else "Show paused.",
        )


def _parse_progress_query_state(
    config,
    *,
    hide_upcoming: str,
    show_paused: str,
    show_dropped: str,
    sort_mode: str,
    sort_direction: str,
) -> _ProgressPageState:
    return _make_progress_state(
        hide_upcoming=parse_bool_flag(hide_upcoming, config.hide_upcoming_in_progress),
        show_paused=parse_bool_flag(show_paused, getattr(config, "show_paused_in_progress", False)),
        show_dropped=parse_bool_flag(show_dropped, config.show_dropped_in_progress),
        sort_mode=normalize_progress_sort_mode(
            sort_mode,
            getattr(config, "web_progress_sort_mode", DEFAULT_PROGRESS_SORT_MODE),
        ),
        sort_direction=normalize_progress_sort_direction(
            sort_direction,
            getattr(config, "web_progress_sort_direction", DEFAULT_PROGRESS_SORT_DIRECTION),
        ),
    )


def _parse_progress_form_state(form) -> _ProgressPageState:
    return _make_progress_state(
        hide_upcoming=parse_bool_flag(str(form.get("hide_upcoming", ""))),
        show_paused=parse_bool_flag(str(form.get("show_paused", ""))),
        show_dropped=parse_bool_flag(str(form.get("show_dropped", ""))),
        sort_mode=normalize_progress_sort_mode(str(form.get("sort", ""))),
        sort_direction=normalize_progress_sort_direction(str(form.get("direction", ""))),
    )


def _make_progress_state(
    *,
    hide_upcoming: bool,
    show_paused: bool,
    show_dropped: bool,
    sort_mode: str,
    sort_direction: str,
) -> _ProgressPageState:
    if show_dropped:
        show_paused = False
    elif show_paused:
        show_dropped = False
    return _ProgressPageState(
        hide_upcoming=hide_upcoming,
        show_paused=show_paused,
        show_dropped=show_dropped,
        sort_mode=normalize_progress_sort_mode(sort_mode),
        sort_direction=normalize_progress_sort_direction(sort_direction),
    )


def _find_progress_item(services: ServiceContainer, trakt_id: int, *, state: _ProgressPageState):
    items = services.progress.dashboard_progress(
        view=state.view,
        sort_mode=state.sort_mode,
        descending=state.descending,
        limit=None,
    )
    return next((item for item in items if item.trakt_id == trakt_id), None)


def _find_tmdb_progress_item(services: ServiceContainer, tmdb_id: int, *, state: _ProgressPageState):
    if not _tmdb_progress_enabled(services):
        return None
    reader = getattr(services.tmdb_catalog, "local_progress_items", None)
    if not callable(reader):
        return None
    items = reader(
        view=state.view,
        sort_mode=state.sort_mode,
        descending=state.descending,
        limit=None,
    )
    return next((item for item in items if int(getattr(item, "tmdb_id", 0) or 0) == int(tmdb_id)), None)


def _load_progress_items(
    services: ServiceContainer,
    *,
    state: _ProgressPageState,
):
    if _tmdb_progress_enabled(services):
        reader = getattr(services.tmdb_catalog, "local_progress_items", None)
        items = reader(
            view=state.view,
            sort_mode=state.sort_mode,
            descending=state.descending,
            limit=None,
        ) if callable(reader) else []
    else:
        items = services.progress.dashboard_progress(
            view=state.view,
            sort_mode=state.sort_mode,
            descending=state.descending,
            limit=None,
        )
    items = filter_progress_items(
        items,
        hide_upcoming=state.hide_upcoming,
        show_paused=state.show_paused,
        show_dropped=state.show_dropped,
    )
    unseen_episode_ids = services.notifications.unseen_episode_ids()
    new_items = [
        item for item in items
        if state.view == "active"
        and item.next_episode is not None
        and item.next_episode.trakt_id in unseen_episode_ids
        and (
            item.next_episode.first_aired is None
            or progress_has_released_next_episode(item)
        )
        and not item.is_dropped
        and not getattr(item, "is_paused", False)
    ]
    progress_items = [
        item for item in items
        if item not in new_items
    ]
    new_items = new_items[:_PROGRESS_PAGE_LIMIT]
    progress_items = progress_items[:max(0, _PROGRESS_PAGE_LIMIT - len(new_items))]
    return [*new_items, *progress_items], new_items, progress_items


def _tmdb_progress_enabled(services: ServiceContainer) -> bool:
    return _tmdb_preview_mode(services)


def _tmdb_preview_mode(services: ServiceContainer) -> bool:
    return normalize_catalog_provider_mode(
        getattr(services.auth.config, "catalog_provider_mode", "trakt")
    ) == "tmdb_preview"


def _progress_card_key(item) -> str:
    tmdb_id = int(getattr(item, "tmdb_id", 0) or 0)
    if str(getattr(item, "provider", "trakt")) == "tmdb" and tmdb_id > 0:
        return f"progress:tmdb:{tmdb_id}"
    return f"progress:{int(item.trakt_id)}"


def _sort_combined_progress_items(items: list, *, sort_mode: str, descending: bool) -> list:
    if sort_mode == "last_watched":
        def raw_value(item):
            return getattr(item, "last_watched_at", None)
    elif sort_mode == "release_year":
        def raw_value(item):
            return getattr(item, "title_year", None)
    else:
        def raw_value(item):
            return getattr(getattr(item, "next_episode", None), "first_aired", None)

    def comparable(value):
        if isinstance(value, datetime):
            known = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            return known.timestamp()
        return value

    known = [item for item in items if raw_value(item) is not None]
    unknown = [item for item in items if raw_value(item) is None]
    known.sort(key=lambda item: comparable(raw_value(item)), reverse=bool(descending))
    unknown.sort(
        key=lambda item: (
            str(getattr(item, "title", "")).casefold(),
            int(getattr(item, "tmdb_id", 0) or getattr(item, "trakt_id", 0) or 0),
        )
    )
    return [*known, *unknown]


def _normalize_card_keys(raw_keys) -> list[str]:
    return [
        str(key)
        for key in dict.fromkeys(raw_keys if isinstance(raw_keys, list) else [])
        if isinstance(key, str) and key.strip()
    ]


def _build_progress_bucket_tasks(
    services: ServiceContainer,
    items_by_key: dict[str, object],
    card_keys: list[str],
    *,
    priority: int,
    trigger: str = TRIGGER_VIEWPORT,
    title_requested_parts=(),
    episode_requested_parts=(),
) -> list:
    tasks: list = []
    items = [
        items_by_key[key]
        for key in card_keys
        if key in items_by_key
        and str(getattr(items_by_key[key], "provider", "trakt")) != "tmdb"
    ]
    title_keys = set(
        services.progress.select_title_enrich_keys(
            items,
            trigger=trigger,
            requested_parts=title_requested_parts,
        )
    )
    episode_keys = set(
        services.progress.select_episode_enrich_keys(
            items,
            trigger=trigger,
            requested_parts=episode_requested_parts,
        )
    )
    for card_key in card_keys:
        item = items_by_key.get(card_key)
        if item is None:
            continue
        if str(getattr(item, "provider", "trakt")) == "tmdb":
            continue
        title_candidate = (int(item.trakt_id), "show")
        if title_candidate in title_keys:
            tasks.append(
                build_progress_title_task(
                    title_key=card_key,
                    trakt_id=int(item.trakt_id),
                    title_type="show",
                    priority=priority,
                    trigger=trigger,
                    requested_parts=title_requested_parts,
                )
            )
        next_episode = item.next_episode
        if next_episode is None:
            continue
        episode_candidate = (int(item.trakt_id), int(next_episode.season), int(next_episode.number))
        if episode_candidate in episode_keys:
            tasks.append(
                build_progress_episode_task(
                    title_key=card_key,
                    show_trakt_id=int(item.trakt_id),
                    season=int(next_episode.season),
                    episode=int(next_episode.number),
                    priority=priority,
                    trigger=trigger,
                    requested_parts=episode_requested_parts,
                )
            )
    return tasks


def _select_stale_progress_rating_card_keys(items_by_key: dict[str, object], card_keys: list[str]) -> list[str]:
    result: list[str] = []
    for card_key in card_keys:
        item = items_by_key.get(card_key)
        if item is None:
            continue
        if ratings_refresh_due(
            getattr(item, "title_ratings_status", "unknown"),
            getattr(item, "title_ratings_refreshed_at", None),
            asset_kind=ASSET_KIND_TITLE_RATINGS,
            ready_ttl_seconds=TITLE_RATINGS_READY_REFRESH_SECONDS,
        ):
            result.append(card_key)
            continue
        next_episode = getattr(item, "next_episode", None)
        if next_episode is None:
            continue
        if ratings_refresh_due(
            getattr(next_episode, "trakt_details_status", "unknown"),
            getattr(next_episode, "trakt_details_refreshed_at", None),
            asset_kind=ASSET_KIND_EPISODE_RATINGS,
            ready_ttl_seconds=EPISODE_RATINGS_READY_REFRESH_SECONDS,
        ):
            result.append(card_key)
    return list(dict.fromkeys(result))
