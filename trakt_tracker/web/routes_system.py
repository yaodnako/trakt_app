from __future__ import annotations

import asyncio
import mimetypes
import shutil
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

from sqlalchemy import func, select

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import ConfigStore, get_app_data_dir, normalize_utc_offset
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.persistence.models import EpisodeCache, Title
from trakt_tracker.web.app_shared import image_cache_suffix
from trakt_tracker.web.viewmodels import parse_bool_flag


_image_warm_lock = Lock()
_image_warm_running: set[str] = set()


def register_system_routes(app, *, render, template_filters) -> None:
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/progress", status_code=302)

    @app.get("/cached-image")
    async def cached_image(request: Request, url: str = "") -> Response:
        target_url = url.strip()
        if not target_url:
            return Response(status_code=404)
        services: ServiceContainer = request.app.state.services
        cache: BinaryCache = request.app.state.image_cache
        payload = cache.get_bytes(target_url, max(1, int(services.auth.config.cache_ttl_hours)))
        media_type, _ = mimetypes.guess_type(target_url)
        if payload is not None:
            return Response(content=payload, media_type=media_type or "image/jpeg")
        stale_payload = cache.get_any_bytes(target_url)
        if stale_payload is not None:
            return Response(content=stale_payload, media_type=media_type or "image/jpeg")
        fetched = await asyncio.to_thread(_fetch_and_cache_image, cache, target_url, 5)
        if fetched is not None:
            payload, content_type = fetched
            return Response(content=payload, media_type=content_type or media_type or "image/jpeg")
        _warm_image_cache_in_background(cache, target_url, timeout=5)
        return RedirectResponse(url=target_url, status_code=307)

    @app.get("/notification-sound")
    async def notification_sound(request: Request) -> Response:
        sound_path = Path(str(request.app.state.services.auth.config.notification_sound_path or "")).expanduser()
        if not sound_path.exists() or not sound_path.is_file():
            return Response(status_code=404)
        media_type, _ = mimetypes.guess_type(str(sound_path))
        return FileResponse(sound_path, media_type=media_type or "audio/mpeg")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, flash: str = "") -> HTMLResponse:
        config = request.app.state.services.auth.config
        bg_tasks = request.app.state.bg_tasks
        return render(
            request,
            "settings.html",
            {
                "page_title": "Settings",
                "flash": flash,
                "config": config,
                "imdb_sync_running": bg_tasks.is_running("imdb_manual_sync") or bg_tasks.is_running("imdb_auto_sync"),
                "imdb_sync_status": request.app.state.services.sync.imdb_dataset_status(),
            },
        )

    @app.post("/settings")
    async def settings_save(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        config = services.auth.update_config(
            str(form.get("client_id", "") or ""),
            str(form.get("client_secret", "") or ""),
            str(form.get("redirect_uri", "") or ""),
            str(form.get("tmdb_api_key", "") or ""),
            str(form.get("tmdb_read_access_token", "") or ""),
            str(form.get("kinopoisk_api_key", "") or ""),
        )
        try:
            cache_ttl_hours = int(str(form.get("cache_ttl_hours", config.cache_ttl_hours) or config.cache_ttl_hours))
        except ValueError:
            cache_ttl_hours = config.cache_ttl_hours
        try:
            poll_interval_minutes = int(str(form.get("poll_interval_minutes", config.poll_interval_minutes) or config.poll_interval_minutes))
        except ValueError:
            poll_interval_minutes = config.poll_interval_minutes
        try:
            notification_repeat_minutes = int(
                str(form.get("notification_repeat_minutes", config.notification_repeat_minutes) or config.notification_repeat_minutes)
            )
        except ValueError:
            notification_repeat_minutes = config.notification_repeat_minutes
        try:
            imdb_auto_sync_interval_minutes = int(
                str(form.get("imdb_auto_sync_interval_minutes", config.imdb_auto_sync_interval_minutes) or config.imdb_auto_sync_interval_minutes)
            )
        except ValueError:
            imdb_auto_sync_interval_minutes = config.imdb_auto_sync_interval_minutes
        notification_sound_path = str(form.get("notification_sound_path", config.notification_sound_path) or "").strip()
        uploaded_sound = form.get("notification_sound_file")
        if uploaded_sound is not None and getattr(uploaded_sound, "filename", ""):
            filename = Path(str(uploaded_sound.filename)).name
            destination_dir = get_app_data_dir() / "notification_sounds"
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / filename
            with destination.open("wb") as handle:
                shutil.copyfileobj(uploaded_sound.file, handle)
            notification_sound_path = str(destination)
        config.cache_ttl_hours = max(1, min(168, cache_ttl_hours))
        config.poll_interval_minutes = max(5, min(240, poll_interval_minutes))
        config.notification_repeat_minutes = max(1, min(240, notification_repeat_minutes))
        config.imdb_auto_sync_interval_minutes = max(1, min(10080, imdb_auto_sync_interval_minutes))
        config.imdb_auto_sync_interval_hours = max(1, config.imdb_auto_sync_interval_minutes // 60 or 1)
        config.notification_sound_path = notification_sound_path
        config.notifications_enabled = parse_bool_flag(str(form.get("notifications_enabled", "")))
        config.debug_mode = parse_bool_flag(str(form.get("debug_mode", "")))
        config.open_in_embedded_player = parse_bool_flag(str(form.get("open_in_embedded_player", "")))
        config.utc_offset = normalize_utc_offset(str(form.get("utc_offset", config.utc_offset or "+03:00")))
        ConfigStore().save(config)
        template_filters.utc_offset = config.utc_offset
        return RedirectResponse(url="/settings?flash=Settings+saved.", status_code=303)

    @app.post("/settings/imdb-sync")
    async def settings_imdb_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        started = bg_tasks.start(
            "imdb_manual_sync",
            source="IMDb sync (manual)",
            operations=services.operations,
            fn=lambda: services.sync.sync_imdb_dataset(
                force=True,
                status_callback=lambda message: services.operations.publish("IMDb sync", message),
            ),
        )
        flash = "IMDb sync started." if started else "IMDb sync is already running."
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/full-sync")
    async def settings_full_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        if services.enrich_queue.is_running() or bg_tasks.is_running("history_sync") or bg_tasks.is_running("progress_sync"):
            flash = "Sync is waiting for current background tasks to finish."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        services.cache.clear_provider("images")
        started = bg_tasks.start(
            "settings_full_sync",
            source="Full sync",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_full(),
        )
        flash = "Full sync started." if started else "Full sync is already running."
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/sync-missing")
    async def settings_sync_missing(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        if services.enrich_queue.is_running() or bg_tasks.is_running("history_sync") or bg_tasks.is_running("progress_sync"):
            flash = "Sync is waiting for current background tasks to finish."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        started = bg_tasks.start(
            "settings_backfill_sync",
            source="Backfill sync",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_backfill(),
        )
        flash = "Sync started." if started else "Sync is already running."
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/sync-timeout")
    async def settings_sync_timeout(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        if services.enrich_queue.is_running() or bg_tasks.is_running("history_sync") or bg_tasks.is_running("progress_sync"):
            flash = "Sync is waiting for current background tasks to finish."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        started = bg_tasks.start(
            "settings_timeout_sync",
            source="Timeout sync",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_timeout_only(),
        )
        flash = "Timeout sync started." if started else "Timeout sync is already running."
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/sync-repair")
    async def settings_sync_repair(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        if services.enrich_queue.is_running() or bg_tasks.is_running("history_sync") or bg_tasks.is_running("progress_sync"):
            flash = "Sync is waiting for current background tasks to finish."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        started = bg_tasks.start(
            "settings_repair_sync",
            source="Repair sync",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_repair(),
        )
        flash = "Repair sync started." if started else "Repair sync is already running."
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.get("/settings/imdb-sync-status")
    async def settings_imdb_sync_status(request: Request, after: int = 0) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        raw_events = [
            event
            for event in services.operations.list_after(after)
            if str(event.get("source", "")).startswith("IMDb sync")
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
                "running": bg_tasks.is_running("imdb_manual_sync") or bg_tasks.is_running("imdb_auto_sync"),
                "status": services.sync.imdb_dataset_status(),
                "progress_message": latest_progress,
                "events": recent_events[-6:],
            }
        )

    @app.get("/settings/refresh-status")
    async def settings_refresh_status(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        raw_events = services.operations.list_after(0)
        history_events = [event for event in raw_events if str(event.get("source", "")).startswith("History sync")]
        progress_events = [event for event in raw_events if str(event.get("source", "")).startswith("Progress sync")]
        refresh_events = [
            event
            for event in raw_events
            if str(event.get("source", "")).startswith(("Full sync", "Backfill sync", "Timeout sync", "Repair sync"))
        ]
        last_history_progress = ""
        last_history_message = ""
        for event in history_events:
            message = str(event.get("message", "") or "")
            if "%" in message:
                last_history_progress = message
            if message:
                last_history_message = message
        last_progress_message = ""
        for event in progress_events:
            message = str(event.get("message", "") or "")
            if message:
                last_progress_message = message
        last_refresh_message = ""
        for event in refresh_events:
            message = str(event.get("message", "") or "")
            if message:
                last_refresh_message = message
        db = services.sync._db
        with db.session() as session:
            title_poster_base = select(Title).where(Title.title_type.in_(("show", "movie")))
            title_total = int(session.scalar(select(func.count()).select_from(title_poster_base.subquery())) or 0)
            poster_ready = int(
                session.scalar(
                    select(func.count()).select_from(Title).where(
                        Title.title_type.in_(("show", "movie")),
                        Title.poster_status == "ready",
                        Title.poster_url != "",
                    )
                )
                or 0
            )
            poster_no_data = int(
                session.scalar(
                    select(func.count()).select_from(Title).where(
                        Title.title_type.in_(("show", "movie")),
                        Title.poster_status == "checked_no_data",
                    )
                )
                or 0
            )
            poster_retry = int(
                session.scalar(
                    select(func.count()).select_from(Title).where(
                        Title.title_type.in_(("show", "movie")),
                        Title.poster_status == "retryable_failure",
                    )
                )
                or 0
            )
            episode_total = int(session.scalar(select(func.count()).select_from(EpisodeCache)) or 0)
            still_ready = int(
                session.scalar(
                    select(func.count()).select_from(EpisodeCache).where(EpisodeCache.still_status == "ready", EpisodeCache.still_url != "")
                )
                or 0
            )
            still_no_data = int(
                session.scalar(select(func.count()).select_from(EpisodeCache).where(EpisodeCache.still_status == "checked_no_data")) or 0
            )
            still_retry = int(
                session.scalar(select(func.count()).select_from(EpisodeCache).where(EpisodeCache.still_status == "retryable_failure")) or 0
            )
        poster_unknown = max(0, title_total - poster_ready - poster_no_data - poster_retry)
        still_unknown = max(0, episode_total - still_ready - still_no_data - still_retry)
        return JSONResponse(
            {
                "running": {
                    "history_sync": bg_tasks.is_running("history_sync"),
                    "progress_sync": bg_tasks.is_running("progress_sync"),
                    "enrich_queue": services.enrich_queue.is_running(),
                    "full_sync": bg_tasks.is_running("settings_full_sync"),
                    "backfill_sync": bg_tasks.is_running("settings_backfill_sync"),
                    "timeout_sync": bg_tasks.is_running("settings_timeout_sync"),
                    "repair_sync": bg_tasks.is_running("settings_repair_sync"),
                },
                "history": {
                    "progress_message": last_history_progress,
                    "last_message": last_history_message,
                },
                "progress": {
                    "last_message": last_progress_message,
                },
                "refresh": {
                    "last_message": last_refresh_message,
                },
                "posters": {
                    "total": title_total,
                    "ready": poster_ready,
                    "checked_no_data": poster_no_data,
                    "retryable_failure": poster_retry,
                    "unknown": poster_unknown,
                },
                "stills": {
                    "total": episode_total,
                    "ready": still_ready,
                    "checked_no_data": still_no_data,
                    "retryable_failure": still_retry,
                    "unknown": still_unknown,
                },
            }
        )

    @app.get("/notifications/poll")
    async def notifications_poll(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        if not services.auth.is_authorized():
            return JSONResponse({"items": []})
        try:
            items = services.notifications.poll_upcoming(send_native=False)
        except Exception:
            items = []
        if items:
            bg_tasks.start(
                "progress_sync",
                source="Progress sync (notification)",
                operations=services.operations,
                fn=lambda: services.progress.sync_progress(dropped_only=False),
            )
        return JSONResponse({"items": items})

    @app.get("/debug/events")
    async def debug_events(request: Request, after: int = 0) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        return JSONResponse({"events": services.operations.list_after(after)})


def _warm_image_cache_in_background(cache: BinaryCache, target_url: str, *, timeout: int) -> None:
    with _image_warm_lock:
        if target_url in _image_warm_running:
            return
        _image_warm_running.add(target_url)

    def runner() -> None:
        try:
            _fetch_and_cache_image(cache, target_url, timeout)
        except Exception:
            pass
        finally:
            with _image_warm_lock:
                _image_warm_running.discard(target_url)

    Thread(target=runner, daemon=True).start()


def _fetch_and_cache_image(cache: BinaryCache, target_url: str, timeout: int) -> tuple[bytes, str] | None:
    try:
        upstream_request = UrlRequest(
            target_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        with urlopen(upstream_request, timeout=timeout) as upstream_response:
            fetched = upstream_response.read()
            content_type = upstream_response.headers.get("Content-Type", "")
        if not fetched:
            return None
        cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(target_url, content_type))
        return fetched, content_type
    except Exception:
        return None
