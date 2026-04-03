from __future__ import annotations

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
from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import ConfigStore
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.viewmodels import (
    filter_progress_items,
    parse_bool_flag,
    parse_progress_year,
)


def register_progress_routes(app, *, render, progress_redirect) -> None:
    @app.get("/progress", response_class=HTMLResponse)
    async def progress_page(
        request: Request,
        hide_upcoming: str = "",
        show_dropped: str = "",
        min_year: str = "",
        use_year_filter: str = "",
        flash: str = "",
        rate_trakt_id: int | None = None,
        rate_season: int | None = None,
        rate_episode: int | None = None,
        rate_title: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        config = services.auth.config
        hide_upcoming_value = parse_bool_flag(hide_upcoming, config.hide_upcoming_in_progress)
        show_dropped_value = parse_bool_flag(show_dropped, config.show_dropped_in_progress)
        min_year_value = parse_progress_year(min_year)
        if min_year_value is None:
            min_year_value = config.web_progress_min_year
        use_year_filter_value = parse_bool_flag(use_year_filter, config.web_progress_year_filter_enabled)
        config_changed = False
        if config.hide_upcoming_in_progress != hide_upcoming_value:
            config.hide_upcoming_in_progress = hide_upcoming_value
            config_changed = True
        if config.show_dropped_in_progress != show_dropped_value:
            config.show_dropped_in_progress = show_dropped_value
            config_changed = True
        if config.web_progress_min_year != min_year_value:
            config.web_progress_min_year = min_year_value
            config_changed = True
        if config.web_progress_year_filter_enabled != use_year_filter_value:
            config.web_progress_year_filter_enabled = use_year_filter_value
            config_changed = True
        if config_changed:
            ConfigStore().save(config)
        items = services.progress.dashboard_progress(dropped_only=show_dropped_value)
        items = filter_progress_items(
            items,
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
        )
        unseen_episode_ids = services.notifications.unseen_episode_ids()
        new_items = [
            item for item in items
            if item.next_episode is not None and item.next_episode.trakt_id in unseen_episode_ids and not item.is_dropped
        ]
        progress_items = [
            item for item in items
            if item.next_episode is None or item.next_episode.trakt_id not in unseen_episode_ids or item.is_dropped
        ]
        return render(
            request,
            "progress.html",
            {
                "page_title": "Progress",
                "new_items": new_items,
                "progress_items": progress_items,
                "unseen_episode_ids": unseen_episode_ids,
                "hide_upcoming": hide_upcoming_value,
                "show_dropped": show_dropped_value,
                "min_year": min_year_value,
                "use_year_filter": use_year_filter_value,
                "progress_sync_running": request.app.state.bg_tasks.is_running("progress_sync"),
                "flash": flash,
                "rate_trakt_id": rate_trakt_id,
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
        hide_upcoming_value = parse_bool_flag(str(payload.get("hide_upcoming", "")), services.auth.config.hide_upcoming_in_progress)
        show_dropped_value = parse_bool_flag(str(payload.get("show_dropped", "")), services.auth.config.show_dropped_in_progress)
        min_year_value = parse_progress_year(str(payload.get("min_year", "")))
        if min_year_value is None:
            min_year_value = services.auth.config.web_progress_min_year
        use_year_filter_value = parse_bool_flag(str(payload.get("use_year_filter", "")), services.auth.config.web_progress_year_filter_enabled)
        viewport_card_keys = _normalize_card_keys(payload.get("viewport_card_keys", []))
        nearby_card_keys = _normalize_card_keys(payload.get("nearby_card_keys", []))
        page_card_keys = _normalize_card_keys(payload.get("page_card_keys", []))
        try:
            queue_after_revision = max(0, int(payload.get("queue_after_revision", 0) or 0))
        except (TypeError, ValueError):
            queue_after_revision = 0
        all_items, new_items, progress_items = _load_progress_items(
            services,
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
        )
        items_by_key = {f"progress:{item.trakt_id}": item for item in all_items}
        current_page_keys = [f"progress:{item.trakt_id}" for item in all_items]
        if not request.app.state.bg_tasks.is_running("progress_sync"):
            services.enrich_queue.submit_progress_refresh(
                viewport_tasks=_build_progress_bucket_tasks(services, items_by_key, viewport_card_keys, priority=1),
                nearby_tasks=_build_progress_bucket_tasks(services, items_by_key, nearby_card_keys, priority=2),
                page_tasks=[],
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
                            "hide_upcoming": hide_upcoming_value,
                            "show_dropped": show_dropped_value,
                            "min_year": min_year_value,
                            "use_year_filter": use_year_filter_value,
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
            }
        )

    @app.post("/progress/sync")
    async def progress_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        form = await request.form()
        hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
        show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
        min_year_value = parse_progress_year(str(form.get("min_year", "")))
        use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
        if services.enrich_queue.is_running():
            return progress_redirect(
                hide_upcoming=hide_upcoming_value,
                show_dropped=show_dropped_value,
                min_year=min_year_value,
                use_year_filter=use_year_filter_value,
                flash="Progress sync is waiting for current enrich tasks to finish.",
            )
        started = bg_tasks.start(
            "progress_sync",
            source="Progress sync (manual full)",
            operations=services.operations,
            fn=lambda: services.progress.sync_progress(dropped_only=show_dropped_value),
        )
        return progress_redirect(
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
            flash="Progress sync started." if started else "Progress sync is already running.",
        )

    @app.get("/progress/{trakt_id}/play")
    async def progress_play(
        request: Request,
        trakt_id: int,
        hide_upcoming: str = "",
        show_dropped: str = "",
        min_year: str = "",
        use_year_filter: str = "",
    ) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        hide_upcoming_value = parse_bool_flag(hide_upcoming, services.auth.config.hide_upcoming_in_progress)
        show_dropped_value = parse_bool_flag(show_dropped, services.auth.config.show_dropped_in_progress)
        min_year_value = parse_progress_year(min_year)
        if min_year_value is None:
            min_year_value = services.auth.config.web_progress_min_year
        use_year_filter_value = parse_bool_flag(use_year_filter, services.auth.config.web_progress_year_filter_enabled)
        current = _find_progress_item(services, trakt_id, dropped_only=show_dropped_value)
        services.operations.publish("Play", f"Play requested: trakt_id={trakt_id}")
        if current is None:
            return progress_redirect(
                hide_upcoming=hide_upcoming_value,
                show_dropped=show_dropped_value,
                min_year=min_year_value,
                use_year_filter=use_year_filter_value,
                flash="Title not found.",
            )
        target_url = services.play.resolve_kinopoisk_url(current.title, domain="net")
        if not target_url:
            return progress_redirect(
                hide_upcoming=hide_upcoming_value,
                show_dropped=show_dropped_value,
                min_year=min_year_value,
                use_year_filter=use_year_filter_value,
                flash=f"Kinopoisk not found for {current.title}.",
            )
        return RedirectResponse(url=target_url, status_code=302)

    @app.post("/progress/{trakt_id}/watch")
    async def progress_watch(request: Request, trakt_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        hide_upcoming_value, show_dropped_value, min_year_value, use_year_filter_value = _parse_progress_form_state(form)
        current = _find_progress_item(services, trakt_id, dropped_only=show_dropped_value)
        if current is None or current.next_episode is None:
            return progress_redirect(
                hide_upcoming=hide_upcoming_value,
                show_dropped=show_dropped_value,
                min_year=min_year_value,
                use_year_filter=use_year_filter_value,
                flash="No next episode to mark watched.",
            )
        episode = current.next_episode
        services.operations.publish("Progress action", f"Mark watched: {current.title} S{episode.season:02d}E{episode.number:02d}")
        services.interactions.mark_progress_episode_watched(current, watched_at=datetime.now())
        services.progress.sync_progress([current.trakt_id], dropped_only=show_dropped_value)
        return progress_redirect(
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
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
        hide_upcoming_value, show_dropped_value, min_year_value, use_year_filter_value = _parse_progress_form_state(form)
        current = _find_progress_item(services, trakt_id, dropped_only=show_dropped_value)
        if current is None or current.next_episode is None:
            return progress_redirect(
                hide_upcoming=hide_upcoming_value,
                show_dropped=show_dropped_value,
                min_year=min_year_value,
                use_year_filter=use_year_filter_value,
                flash="No released episode to mark seen.",
            )
        episode = current.next_episode
        try:
            services.interactions.mark_progress_episode_seen(current, now=datetime.now(tz=UTC))
        except RuntimeError as exc:
            return progress_redirect(
                hide_upcoming=hide_upcoming_value,
                show_dropped=show_dropped_value,
                min_year=min_year_value,
                use_year_filter=use_year_filter_value,
                flash=str(exc),
            )
        services.operations.publish("Progress action", f"Marked seen: {current.title} S{episode.season:02d}E{episode.number:02d}")
        return progress_redirect(
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
            flash=f"Marked {current.title} {episode.season:02d}x{episode.number:02d} seen.",
        )

    @app.post("/progress/rate")
    async def progress_rate(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        hide_upcoming_value, show_dropped_value, min_year_value, use_year_filter_value = _parse_progress_form_state(form)
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
        services.progress.sync_progress([trakt_id], dropped_only=show_dropped_value)
        return progress_redirect(
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
            flash=flash,
        )

    @app.post("/progress/{trakt_id}/drop-toggle")
    async def progress_drop_toggle(request: Request, trakt_id: int) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        hide_upcoming_value, show_dropped_value, min_year_value, use_year_filter_value = _parse_progress_form_state(form)
        current_is_dropped = parse_bool_flag(str(form.get("is_dropped", "")))
        if current_is_dropped:
            services.interactions.set_progress_dropped(trakt_id, dropped=False)
            flash = "Show restored."
        else:
            services.interactions.set_progress_dropped(trakt_id, dropped=True)
            flash = "Show dropped."
        return progress_redirect(
            hide_upcoming=hide_upcoming_value,
            show_dropped=show_dropped_value,
            min_year=min_year_value,
            use_year_filter=use_year_filter_value,
            flash=flash,
        )


def _parse_progress_form_state(form) -> tuple[bool, bool, int | None, bool]:
    hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
    show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
    min_year_value = parse_progress_year(str(form.get("min_year", "")))
    use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
    return hide_upcoming_value, show_dropped_value, min_year_value, use_year_filter_value


def _find_progress_item(services: ServiceContainer, trakt_id: int, *, dropped_only: bool):
    items = services.progress.dashboard_progress(dropped_only=dropped_only)
    return next((item for item in items if item.trakt_id == trakt_id), None)


def _load_progress_items(
    services: ServiceContainer,
    *,
    hide_upcoming: bool,
    show_dropped: bool,
    min_year: int | None,
    use_year_filter: bool,
):
    items = services.progress.dashboard_progress(dropped_only=show_dropped)
    items = filter_progress_items(
        items,
        hide_upcoming=hide_upcoming,
        show_dropped=show_dropped,
        min_year=min_year,
        use_year_filter=use_year_filter,
    )
    unseen_episode_ids = services.notifications.unseen_episode_ids()
    new_items = [
        item for item in items
        if item.next_episode is not None and item.next_episode.trakt_id in unseen_episode_ids and not item.is_dropped
    ]
    progress_items = [
        item for item in items
        if item.next_episode is None or item.next_episode.trakt_id not in unseen_episode_ids or item.is_dropped
    ]
    return [*new_items, *progress_items], new_items, progress_items


def _normalize_card_keys(raw_keys) -> list[str]:
    return [
        str(key)
        for key in dict.fromkeys(raw_keys if isinstance(raw_keys, list) else [])
        if isinstance(key, str) and key.strip()
    ]


def _build_progress_bucket_tasks(services: ServiceContainer, items_by_key: dict[str, object], card_keys: list[str], *, priority: int) -> list:
    tasks: list = []
    items = [items_by_key[key] for key in card_keys if key in items_by_key]
    title_keys = set(services.progress.select_title_enrich_keys(items))
    episode_keys = set(services.progress.select_episode_enrich_keys(items))
    for card_key in card_keys:
        item = items_by_key.get(card_key)
        if item is None:
            continue
        title_candidate = (int(item.trakt_id), "show")
        if title_candidate in title_keys:
            tasks.append(
                build_progress_title_task(
                    title_key=card_key,
                    trakt_id=int(item.trakt_id),
                    title_type="show",
                    priority=priority,
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
                )
            )
    return tasks
