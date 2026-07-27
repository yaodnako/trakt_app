from __future__ import annotations

import asyncio
import mimetypes
import shutil
from pathlib import Path
from urllib.parse import quote, urlsplit

from sqlalchemy import func, select

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from trakt_tracker.application.initial_setup import run_initial_setup
from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.config import (
    ConfigStore,
    get_app_data_dir,
    normalize_kinopoisk_domain_options,
    normalize_kinopoisk_domain_tail,
    normalize_utc_offset,
    resolved_tmdb_api_key,
    resolved_tmdb_read_access_token,
    trakt_credentials_source,
)
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.infrastructure.artwork_cache import is_trusted_image_url, warm_image_cache_in_background
from trakt_tracker.infrastructure.windows_autostart import set_web_tray_autostart
from trakt_tracker.persistence.models import EpisodeCache, Title
from trakt_tracker.profiles import default_setup_state, read_setup_state, recover_interrupted_setup
from trakt_tracker.web.viewmodels import parse_bool_flag


_PENDING_IMAGE_GIF = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000010001000002024401003b"
)


def register_system_routes(app, *, render, template_filters) -> None:
    def safe_return_path(value: str, *, default: str = "/settings") -> str:
        candidate = str(value or "").strip()
        if any(character in candidate for character in ("\r", "\n", "\\")):
            return default
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
            return default
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def with_flash(path: str, message: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}flash={quote(message)}"

    def setup_task_key(services: ServiceContainer) -> str:
        return f"initial_setup:{services.auth.config.active_slug or 'bootstrap'}"

    def setup_task_running(request: Request, services: ServiceContainer) -> bool:
        return request.app.state.bg_tasks.is_running(setup_task_key(services))

    def activate_services(request: Request, slug: str) -> ServiceContainer:
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is None:
            return request.app.state.services
        services = runtime.services if runtime.active_slug == slug else runtime.activate_profile(slug)
        request.app.state.services = services
        template_filters.utc_offset = services.auth.config.utc_offset
        return services

    def start_initial_setup(request: Request, services: ServiceContainer) -> bool:
        if not hasattr(services, "database"):
            return False
        return request.app.state.bg_tasks.start(
            setup_task_key(services),
            source="Initial setup",
            operations=services.operations,
            fn=lambda: run_initial_setup(services),
        )

    def profile_switch_blocked(request: Request, services: ServiceContainer) -> bool:
        bg_tasks = request.app.state.bg_tasks
        any_running = getattr(bg_tasks, "any_running", None)
        if callable(any_running):
            background_busy = bool(any_running())
        elif hasattr(bg_tasks, "has_running_prefix"):
            background_busy = bool(bg_tasks.has_running_prefix(""))
        else:
            background_busy = False
        authorization_running = getattr(services.auth, "authorization_running", lambda: False)()
        return background_busy or services.enrich_queue.is_running() or authorization_running

    def sync_task_flash(bg_tasks, key: str, label: str, started: bool) -> str:
        if not started:
            return f"{label} is already running or queued."
        is_queued = getattr(bg_tasks, "is_queued", None)
        if callable(is_queued) and is_queued(key):
            return f"{label} queued."
        return f"{label} started."

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/progress", status_code=302)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, flash: str = "") -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        state = default_setup_state()
        if hasattr(services, "database"):
            state = recover_interrupted_setup(
                services.database,
                task_running=setup_task_running(request, services),
            )
        if services.auth.is_authorized() and state.get("state") == "complete":
            return RedirectResponse(url="/progress", status_code=302)
        config = services.auth.config
        return render(
            request,
            "setup.html",
            {
                "page_title": "Setup",
                "setup_mode": True,
                "flash": flash,
                "config": config,
                "state": state,
                "profile_slug": config.active_slug,
                "credentials_source": trakt_credentials_source(config),
                "has_tmdb_defaults": bool(resolved_tmdb_api_key(config) or resolved_tmdb_read_access_token(config)),
            },
        )

    @app.get("/setup/status")
    async def setup_status(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        running = setup_task_running(request, services)
        state = default_setup_state()
        if hasattr(services, "database"):
            state = recover_interrupted_setup(services.database, task_running=running)
        return JSONResponse(
            {
                "configured": services.auth.is_configured(),
                "authorized": services.auth.is_authorized(),
                "profile_slug": services.auth.config.active_slug,
                "state": state.get("state", "pending"),
                "stage": state.get("stage", "history"),
                "message": state.get("message", ""),
                "error": state.get("error", ""),
                "running": running,
            }
        )

    @app.post("/setup/credentials")
    async def setup_credentials(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        services.auth.update_config(
            str(form.get("client_id", "") or ""),
            str(form.get("client_secret", "") or ""),
            str(form.get("redirect_uri", services.auth.config.redirect_uri) or services.auth.config.redirect_uri),
            str(form.get("tmdb_api_key", "") or ""),
            str(form.get("tmdb_read_access_token", "") or ""),
        )
        return RedirectResponse(url="/setup?flash=Provider+credentials+saved.", status_code=303)

    @app.post("/setup/sync")
    async def setup_sync(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        if not services.auth.is_authorized():
            return JSONResponse({"detail": "Trakt authorization required"}, status_code=401)
        state = read_setup_state(services.database)
        if state.get("state") == "complete":
            return JSONResponse({"started": False, "state": state})
        started = start_initial_setup(request, services)
        return JSONResponse({"started": started, "state": read_setup_state(services.database)})

    @app.get("/cached-image")
    async def cached_image(request: Request, url: str = "", v: str = "") -> Response:
        target_url = url.strip()
        if not is_trusted_image_url(target_url):
            return Response(status_code=400, headers={"Cache-Control": "no-store"})
        services: ServiceContainer = request.app.state.services
        cache: BinaryCache = request.app.state.image_cache
        payload = cache.get_bytes(target_url, max(1, int(services.auth.config.cache_ttl_hours)))
        media_type, _ = mimetypes.guess_type(target_url)
        if payload is not None:
            return _image_response(target_url, payload, media_type)
        stale_payload = cache.get_any_bytes(target_url)
        if stale_payload is not None:
            image_queue = getattr(services, "image_queue", None)
            if image_queue is not None:
                image_queue.submit(target_url, priority=3)
            else:
                warm_image_cache_in_background(cache, target_url, timeout=5)
            return _image_response(target_url, stale_payload, media_type)
        image_queue = getattr(services, "image_queue", None)
        if image_queue is not None:
            image_queue.submit(target_url, priority=1)
        else:
            warm_image_cache_in_background(cache, target_url, timeout=5)
        return Response(
            content=_PENDING_IMAGE_GIF,
            media_type="image/gif",
            headers={"Cache-Control": "no-store", "X-Trakt-Image-Pending": "1"},
        )

    @app.get("/notification-sound")
    async def notification_sound(request: Request) -> Response:
        sound_path = Path(str(request.app.state.services.auth.config.notification_sound_path or "")).expanduser()
        if not sound_path.exists() or not sound_path.is_file():
            return Response(status_code=404)
        media_type, _ = mimetypes.guess_type(str(sound_path))
        return FileResponse(sound_path, media_type=media_type or "audio/mpeg")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, flash: str = "", next: str = "") -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        config = services.auth.config
        bg_tasks = request.app.state.bg_tasks
        setup_complete = read_setup_state(services.database).get("state") == "complete"
        profiles = []
        if hasattr(services.auth, "has_token"):
            for slug in config.known_profile_slugs:
                active = slug == config.active_slug
                authorized = services.auth.has_token(slug)
                profiles.append(
                    {
                        "slug": slug,
                        "active": active,
                        "authorized": authorized,
                        "reauthorization_required": active and setup_complete and not authorized,
                    }
                )
        return render(
            request,
            "settings.html",
            {
                "page_title": "Settings",
                "flash": flash,
                "config": config,
                "profiles": profiles,
                "reconnect_return_to": safe_return_path(next),
                "credentials_source": trakt_credentials_source(config),
                "trakt_configured": services.auth.is_configured(),
                "tmdb_configured": bool(resolved_tmdb_api_key(config) or resolved_tmdb_read_access_token(config)),
                "kinopoisk_domain_options": normalize_kinopoisk_domain_options(getattr(config, "kinopoisk_domain_options", "")),
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
            normalize_kinopoisk_domain_tail(str(form.get("kinopoisk_domain_tail", "") or "")),
            str(form.get("kinopoisk_domain_options", "") or ""),
        )
        try:
            cache_ttl_hours = int(str(form.get("cache_ttl_hours", config.cache_ttl_hours) or config.cache_ttl_hours))
        except ValueError:
            cache_ttl_hours = config.cache_ttl_hours
        try:
            explore_imdb_scan_page_limit = int(
                str(
                    form.get(
                        "explore_imdb_scan_page_limit",
                        getattr(config, "explore_imdb_scan_page_limit", 10),
                    )
                    or getattr(config, "explore_imdb_scan_page_limit", 10)
                )
            )
        except ValueError:
            explore_imdb_scan_page_limit = getattr(config, "explore_imdb_scan_page_limit", 10)
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
            notification_release_delay_minutes = int(
                str(
                    form.get(
                        "notification_release_delay_minutes",
                        getattr(config, "notification_release_delay_minutes", 120),
                    )
                    or getattr(config, "notification_release_delay_minutes", 120)
                )
            )
        except ValueError:
            notification_release_delay_minutes = getattr(config, "notification_release_delay_minutes", 120)
        try:
            movie_release_notification_delay_minutes = int(
                str(
                    form.get(
                        "movie_release_notification_delay_minutes",
                        getattr(config, "movie_release_notification_delay_minutes", 10080),
                    )
                    or getattr(config, "movie_release_notification_delay_minutes", 10080)
                )
            )
        except ValueError:
            movie_release_notification_delay_minutes = getattr(config, "movie_release_notification_delay_minutes", 10080)
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
        config.explore_imdb_scan_page_limit = max(1, min(100, explore_imdb_scan_page_limit))
        config.poll_interval_minutes = max(5, min(240, poll_interval_minutes))
        config.notification_repeat_minutes = max(1, min(240, notification_repeat_minutes))
        config.notification_release_delay_minutes = max(0, min(10080, notification_release_delay_minutes))
        config.movie_release_notification_delay_minutes = max(0, min(43200, movie_release_notification_delay_minutes))
        config.imdb_auto_sync_interval_minutes = max(1, min(10080, imdb_auto_sync_interval_minutes))
        config.imdb_auto_sync_interval_hours = max(1, config.imdb_auto_sync_interval_minutes // 60 or 1)
        config.notification_sound_path = notification_sound_path
        config.notifications_enabled = parse_bool_flag(str(form.get("notifications_enabled", "")))
        config.debug_mode = parse_bool_flag(str(form.get("debug_mode", "")))
        config.open_in_embedded_player = parse_bool_flag(str(form.get("open_in_embedded_player", "")))
        config.web_hide_spoilers = parse_bool_flag(str(form.get("web_hide_spoilers", "")))
        config.web_portal_start_with_windows = parse_bool_flag(str(form.get("web_portal_start_with_windows", "")))
        try:
            set_web_tray_autostart(config.web_portal_start_with_windows)
        except OSError:
            config.web_portal_start_with_windows = False
        config.utc_offset = normalize_utc_offset(str(form.get("utc_offset", config.utc_offset or "+03:00")))
        runtime = getattr(request.app.state, "runtime", None)
        (runtime.config_store if runtime is not None else ConfigStore()).save(config)
        template_filters.utc_offset = config.utc_offset
        return RedirectResponse(url="/settings?flash=Settings+saved.", status_code=303)

    @app.post("/settings/trakt-authorize")
    async def settings_trakt_authorize(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        return_to = safe_return_path(str(form.get("return_to", "/settings") or "/settings"))
        try:
            slug = await asyncio.to_thread(services.auth.authorize)
        except Exception as exc:
            flash = f"Trakt authorization failed: {exc}"
            return_to = f"/settings?next={quote(return_to)}"
        else:
            services = activate_services(request, slug)
            state = read_setup_state(services.database) if hasattr(services, "database") else {"state": "complete"}
            if state.get("state") != "complete":
                start_initial_setup(request, services)
                return_to = "/setup"
            flash = f"Trakt authorized as {slug}."
        return RedirectResponse(url=with_flash(return_to, flash), status_code=303)

    @app.post("/settings/provider-defaults")
    async def settings_provider_defaults(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        provider = str(form.get("provider", "") or "")
        try:
            services.auth.clear_provider_overrides(provider)
        except ValueError as exc:
            flash = str(exc)
        else:
            flash = f"{provider.upper()} now uses application defaults."
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/profiles/switch")
    async def settings_profile_switch(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        slug = str(form.get("slug", "") or "").strip()
        if profile_switch_blocked(request, services):
            flash = "Profile switch is blocked while background tasks are running."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        if slug not in services.auth.config.known_profile_slugs:
            flash = "Unknown Trakt profile."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        if not services.auth.has_token(slug):
            flash = "Reconnect this Trakt profile before switching."
            return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)
        services = activate_services(request, slug)
        if read_setup_state(services.database).get("state") != "complete":
            return RedirectResponse(url="/setup", status_code=303)
        return RedirectResponse(url=f"/settings?flash={quote(f'Active profile: {slug}.')}", status_code=303)

    @app.post("/settings/profiles/disconnect")
    async def settings_profile_disconnect(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        slug = str(form.get("slug", services.auth.config.active_slug) or "").strip()
        if slug not in services.auth.config.known_profile_slugs:
            return RedirectResponse(url="/settings?flash=Unknown+Trakt+profile.", status_code=303)
        services.auth.disconnect(slug)
        if slug == services.auth.config.active_slug:
            return RedirectResponse(url=f"/setup?flash={quote(f'{slug} disconnected. Profile data was preserved.')}", status_code=303)
        return RedirectResponse(url=f"/settings?flash={quote(f'{slug} disconnected. Profile data was preserved.')}", status_code=303)

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
        started = bg_tasks.start(
            "settings_full_sync",
            source="Trakt data update",
            operations=services.operations,
            fn=lambda: services.sync.sync_trakt_data(),
        )
        flash = sync_task_flash(bg_tasks, "settings_full_sync", "Trakt data update", started)
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/sync-missing")
    async def settings_sync_missing(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        started = bg_tasks.start(
            "settings_backfill_sync",
            source="Metadata backfill",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_backfill(),
        )
        flash = sync_task_flash(bg_tasks, "settings_backfill_sync", "Metadata backfill", started)
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/sync-timeout")
    async def settings_sync_timeout(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        started = bg_tasks.start(
            "settings_timeout_sync",
            source="Metadata retry",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_timeout_only(),
        )
        flash = sync_task_flash(bg_tasks, "settings_timeout_sync", "Metadata retry", started)
        return RedirectResponse(url=f"/settings?flash={quote(flash)}", status_code=303)

    @app.post("/settings/sync-repair")
    async def settings_sync_repair(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks = request.app.state.bg_tasks
        started = bg_tasks.start(
            "settings_repair_sync",
            source="Metadata recheck",
            operations=services.operations,
            fn=lambda: services.sync.sync_assets_repair(),
        )
        flash = sync_task_flash(bg_tasks, "settings_repair_sync", "Metadata recheck", started)
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
            if str(event.get("source", "")).startswith(
                ("Trakt data update", "Metadata backfill", "Metadata retry", "Metadata recheck")
            )
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
            history_last_success = services.sync._sync_state.get_value(session, SyncPolicy.HISTORY_LAST_SYNC_KEY, "")
            history_last_full_reconcile = services.sync._sync_state.get_value(session, SyncPolicy.HISTORY_LAST_FULL_RECONCILE_KEY, "")
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
        queue_status = services.enrich_queue.status_snapshot()
        artwork_loop = getattr(request.app.state, "artwork_cache_warm_loop", None)
        artwork_status = artwork_loop.status_snapshot() if artwork_loop is not None else {}
        config = services.auth.config
        is_active = getattr(bg_tasks, "is_active", bg_tasks.is_running)
        is_queued = getattr(bg_tasks, "is_queued", lambda _key: False)
        return JSONResponse(
            {
                "running": {
                    "history_sync": bg_tasks.is_running("history_sync"),
                    "progress_sync": bg_tasks.is_running("progress_sync"),
                    "enrich_queue": services.enrich_queue.is_running(),
                    "full_sync": is_active("settings_full_sync"),
                    "backfill_sync": is_active("settings_backfill_sync"),
                    "timeout_sync": is_active("settings_timeout_sync"),
                    "repair_sync": is_active("settings_repair_sync"),
                },
                "queued": {
                    "full_sync": is_queued("settings_full_sync"),
                    "backfill_sync": is_queued("settings_backfill_sync"),
                    "timeout_sync": is_queued("settings_timeout_sync"),
                    "repair_sync": is_queued("settings_repair_sync"),
                },
                "history": {
                    "progress_message": last_history_progress,
                    "last_message": last_history_message,
                    "last_success_at": history_last_success,
                    "last_full_reconcile_at": history_last_full_reconcile,
                },
                "trakt": {"authorized": services.auth.is_authorized()},
                "tmdb": {
                    "configured": bool(resolved_tmdb_read_access_token(config) or resolved_tmdb_api_key(config)),
                    "retryable_failure": poster_retry + still_retry,
                },
                "queue": queue_status,
                "artwork": artwork_status,
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
        if not services.auth.is_authorized():
            return JSONResponse({"items": [], "activity_seq": None})
        try:
            items = services.notifications.poll_upcoming(send_native=False)
        except Exception:
            items = []
        activity_seq = services.notifications.record_activity(items) if items else None
        return JSONResponse({"items": items, "activity_seq": activity_seq})

    @app.get("/notifications/activity")
    async def notification_activity(request: Request, after: int = 0) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        if not services.auth.is_authorized():
            return JSONResponse(
                {
                    "events": [],
                    "seq": services.notifications.current_activity_seq(),
                    "pending_sources": [],
                }
            )
        return JSONResponse(
            {
                "events": services.notifications.activity_after(after),
                "seq": services.notifications.current_activity_seq(),
                "pending_sources": services.notifications.pending_sources(),
            }
        )

    @app.get("/debug/events")
    async def debug_events(request: Request, after: int = 0) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        return JSONResponse({"events": services.operations.list_after(after)})


def _image_media_type(target_url: str, payload: bytes, guessed_media_type: str | None = None) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    stripped = payload[:128].lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml"
    if target_url.lower().split("?", 1)[0].endswith(".webp"):
        return "image/webp"
    return guessed_media_type or "image/jpeg"


def _image_response(target_url: str, payload: bytes, guessed_media_type: str | None = None) -> Response:
    return Response(
        content=payload,
        media_type=_image_media_type(target_url, payload, guessed_media_type),
        headers={"Cache-Control": "no-store"},
    )
