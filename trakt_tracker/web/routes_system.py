from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import ConfigStore, normalize_utc_offset
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.web.app_shared import image_cache_suffix
from trakt_tracker.web.viewmodels import parse_bool_flag


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
        try:
            upstream_request = UrlRequest(
                target_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            with urlopen(upstream_request, timeout=20) as upstream_response:
                fetched = upstream_response.read()
                content_type = upstream_response.headers.get("Content-Type", "")
            if fetched:
                resolved_media_type = media_type or content_type.split(";", 1)[0].strip() or "image/jpeg"
                cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(target_url, content_type))
                return Response(content=fetched, media_type=resolved_media_type)
        except Exception:
            pass
        return RedirectResponse(url=target_url, status_code=307)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, flash: str = "") -> HTMLResponse:
        config = request.app.state.services.auth.config
        return render(
            request,
            "settings.html",
            {
                "page_title": "Settings",
                "flash": flash,
                "config": config,
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
            imdb_auto_sync_interval_hours = int(
                str(form.get("imdb_auto_sync_interval_hours", config.imdb_auto_sync_interval_hours) or config.imdb_auto_sync_interval_hours)
            )
        except ValueError:
            imdb_auto_sync_interval_hours = config.imdb_auto_sync_interval_hours
        config.cache_ttl_hours = max(1, min(168, cache_ttl_hours))
        config.poll_interval_minutes = max(5, min(240, poll_interval_minutes))
        config.imdb_auto_sync_interval_hours = max(1, min(168, imdb_auto_sync_interval_hours))
        config.notifications_enabled = parse_bool_flag(str(form.get("notifications_enabled", "")))
        config.debug_mode = parse_bool_flag(str(form.get("debug_mode", "")))
        config.open_in_embedded_player = parse_bool_flag(str(form.get("open_in_embedded_player", "")))
        config.utc_offset = normalize_utc_offset(str(form.get("utc_offset", config.utc_offset or "+03:00")))
        ConfigStore().save(config)
        template_filters.utc_offset = config.utc_offset
        return RedirectResponse(url="/settings?flash=Settings+saved.", status_code=303)

    @app.get("/notifications/poll")
    async def notifications_poll(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        if not services.auth.is_authorized():
            return JSONResponse({"items": []})
        try:
            items = services.notifications.poll_upcoming(send_native=False)
        except Exception:
            items = []
        return JSONResponse({"items": items})

    @app.get("/debug/events")
    async def debug_events(request: Request, after: int = 0) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        return JSONResponse({"events": services.operations.list_after(after)})
