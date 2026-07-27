from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import ConfigStore, format_local_datetime, get_app_data_dir
from trakt_tracker.formatting import format_compact_votes, format_rating_with_votes
from trakt_tracker.infrastructure.artwork_cache import tmdb_episode_preview_url
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.infrastructure.trakt.client import TraktReauthorizationRequired
from trakt_tracker.profiles import read_setup_state, recover_interrupted_setup
from trakt_tracker.startup_profile import StartupProfiler
from trakt_tracker.web.routes_catalog import register_catalog_routes
from trakt_tracker.web.routes_history import register_history_routes
from trakt_tracker.web.routes_progress import register_progress_routes
from trakt_tracker.web.routes_ratings import register_rating_routes
from trakt_tracker.web.routes_system import register_system_routes
from trakt_tracker.web.runtime import PortalRuntime
from trakt_tracker.web.security import portal_security_middleware
from trakt_tracker.web.viewmodels import (
    format_release_distance,
    progress_effective_aired,
    progress_effective_percent,
    progress_query_string,
    progress_episode_rating_chip,
    progress_rating_chip,
    progress_recent_release,
    progress_skipped_count,
)
from trakt_tracker.version import app_version


_RUNTIME_LOG_LOCK = Lock()
_RUNTIME_LOG_MAX_BYTES = 2 * 1024 * 1024
_RUNTIME_LOG_BACKUPS = 3


def _append_rotating_runtime_log(path: Path, line: str) -> None:
    """Append a bounded local diagnostic log without hiding write failures."""
    try:
        encoded_size = len(line.encode("utf-8"))
        with _RUNTIME_LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + encoded_size > _RUNTIME_LOG_MAX_BYTES:
                for index in range(_RUNTIME_LOG_BACKUPS, 0, -1):
                    source = path.with_name(f"{path.name}.{index}")
                    target = path.with_name(f"{path.name}.{index + 1}")
                    if source.exists():
                        if index == _RUNTIME_LOG_BACKUPS:
                            source.unlink()
                        else:
                            source.replace(target)
                path.replace(path.with_name(f"{path.name}.1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError as exc:
        logging.getLogger("trakt_tracker.runtime").warning("Could not write runtime log %s: %s", path, exc)


class _TemplateFilters:
    utc_offset = "+03:00"

    @staticmethod
    def format_compact_votes(value: int | None) -> str:
        return format_compact_votes(value)

    @staticmethod
    def format_rating_with_votes(rating: float | None, votes: int | None) -> str:
        return format_rating_with_votes(rating, votes)

    @staticmethod
    def format_dt(value) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return format_local_datetime(value, _TemplateFilters.utc_offset)
        return str(value)

    @staticmethod
    def season_episode_label(
        season: int | None,
        episode: int | None,
        imdb_season: int | None = None,
        imdb_episode: int | None = None,
    ) -> str:
        if season is None or episode is None:
            return ""
        label = f"S{season:02d}E{episode:02d}"
        if (
            imdb_season is not None
            and imdb_episode is not None
            and (int(imdb_season), int(imdb_episode)) != (int(season), int(episode))
        ):
            label += f" (S{int(imdb_season):02d}E{int(imdb_episode):02d})"
        return label

    @staticmethod
    def release_distance(value) -> str:
        return format_release_distance(value if isinstance(value, datetime) else None)


class _IMDbAutoSyncLoop:
    def __init__(self, app: FastAPI, *, poll_interval_seconds: float = 30.0) -> None:
        self._app = app
        self._poll_interval_seconds = max(5.0, float(poll_interval_seconds))
        self._stop_event = Event()
        self._thread = Thread(target=self._run, name="web-imdb-auto-sync", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            services: ServiceContainer | None = None
            try:
                services = self._app.state.services
                if services is None:
                    raise RuntimeError("Profile services are unavailable")
                active_services: ServiceContainer = services
                bg_tasks = self._app.state.bg_tasks
                interval_minutes = max(1, int(active_services.auth.config.imdb_auto_sync_interval_minutes or 1))
                if active_services.sync.should_auto_sync_imdb_dataset(interval_minutes):
                    bg_tasks.start(
                        "imdb_auto_sync",
                        source="IMDb sync (auto)",
                        operations=active_services.operations,
                        fn=lambda: active_services.sync.maybe_sync_imdb_dataset(
                            interval_minutes,
                            status_callback=lambda message: active_services.operations.publish("IMDb sync", message),
                        ),
                    )
            except Exception as exc:
                logging.getLogger("trakt_tracker.runtime").exception("IMDb auto-sync loop failed")
                if services is not None:
                    services.operations.publish("IMDb sync", f"IMDb auto-sync loop failed: {type(exc).__name__}: {exc}")
            self._stop_event.wait(self._poll_interval_seconds)


class _TraktEpisodeRatingsRefreshLoop:
    def __init__(self, app: FastAPI, *, poll_interval_seconds: float = 1800.0, batch_size: int = 200) -> None:
        self._app = app
        self._poll_interval_seconds = max(300.0, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._stop_event = Event()
        self._thread = Thread(target=self._run, name="web-trakt-episode-ratings-refresh", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            services: ServiceContainer | None = None
            try:
                services = self._app.state.services
                if services is None:
                    raise RuntimeError("Profile services are unavailable")
                bg_tasks = self._app.state.bg_tasks
                if not bg_tasks.has_running_prefix(
                    "history_sync",
                    "progress_sync",
                    "settings_",
                ):
                    services.sync.enqueue_due_background_trakt_episode_ratings(limit=self._batch_size)
            except Exception as exc:
                logging.getLogger("trakt_tracker.runtime").exception("Trakt episode rating refresh loop failed")
                if services is not None:
                    services.operations.publish(
                        "Episode ratings",
                        f"Episode ratings refresh loop failed: {type(exc).__name__}: {exc}",
                    )
            self._stop_event.wait(self._poll_interval_seconds)


class _ArtworkCacheWarmLoop:
    def __init__(self, app: FastAPI, *, poll_interval_seconds: float = 60.0, batch_size: int = 80) -> None:
        self._app = app
        self._poll_interval_seconds = max(30.0, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._stop_event = Event()
        self._status_lock = Lock()
        self._last_status: dict = {}
        self._thread = Thread(target=self._run, name="web-artwork-cache-warm", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._run_once()
            self._stop_event.wait(self._poll_interval_seconds)

    def _run_once(self) -> None:
        started = perf_counter()
        services: ServiceContainer | None = None
        try:
            services = self._app.state.services
            if services is None:
                raise RuntimeError("Profile services are unavailable")
            bg_tasks = self._app.state.bg_tasks
            profile_write_running = getattr(bg_tasks, "has_running_profile_write", None)
            busy = (
                bool(profile_write_running())
                if callable(profile_write_running)
                else bg_tasks.has_running_prefix(
                    "settings_full_sync",
                    "settings_backfill_sync",
                    "settings_timeout_sync",
                    "settings_repair_sync",
                )
            )
            if busy:
                self._set_status({"status": "skipped", "at": datetime.now(tz=UTC).isoformat(), "error": ""})
                self._write_log(f"skipped=profile_workflow duration_ms={(perf_counter() - started) * 1000.0:.1f}")
                return
            result = services.sync.warm_missing_artwork_cache(limit=self._batch_size, timeout=5, max_workers=4)
            failed_urls = list(result.get("failed_urls") or [])
            self._set_status(
                {
                    "status": "ok" if not result.get("failed") else "partial",
                    "at": datetime.now(tz=UTC).isoformat(),
                    "scanned": int(result.get("scanned", 0)),
                    "selected": int(result.get("selected", 0)),
                    "warmed": int(result.get("warmed", 0)),
                    "failed": int(result.get("failed", 0)),
                    "duration_ms": float(result.get("duration_ms", (perf_counter() - started) * 1000.0)),
                    "error": (f"Failed to cache {failed_urls[-1]}" if failed_urls else ""),
                }
            )
            self._write_log(
                f"scanned={result.get('scanned', 0)} selected={result.get('selected', 0)} "
                f"warmed={result.get('warmed', 0)} failed={result.get('failed', 0)} "
                f"duration_ms={result.get('duration_ms', (perf_counter() - started) * 1000.0):.1f}"
            )
        except Exception as exc:
            message = f"error={type(exc).__name__}: {exc} duration_ms={(perf_counter() - started) * 1000.0:.1f}"
            self._set_status({"status": "failed", "at": datetime.now(tz=UTC).isoformat(), "error": f"{type(exc).__name__}: {exc}"})
            self._write_log(message)
            if services is not None:
                services.operations.publish("Artwork cache", f"Artwork cache warm failed: {message}")

    def status_snapshot(self) -> dict:
        with self._status_lock:
            return dict(self._last_status)

    def _set_status(self, status: dict) -> None:
        with self._status_lock:
            self._last_status = dict(status)

    @staticmethod
    def _write_log(message: str) -> None:
        path = get_app_data_dir() / "web_artwork_cache_warm.log"
        _append_rotating_runtime_log(path, f"{datetime.now(tz=UTC).isoformat()} {message}\n")


def _build_runtime_with_profiling(config_store: ConfigStore | None = None) -> PortalRuntime:
    profile_path = get_app_data_dir() / "web_startup.log"
    profiler = StartupProfiler(profile_path)
    config_store = config_store or ConfigStore()
    profiler.mark("config store ready")
    runtime = PortalRuntime(config_store)
    profiler.mark("active profile services built")
    profiler.finish("web app ready")
    return runtime


def _build_templates() -> Jinja2Templates:
    templates_dir = Path(__file__).with_name("templates")
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["compact_votes"] = _TemplateFilters.format_compact_votes
    templates.env.filters["rating_with_votes"] = _TemplateFilters.format_rating_with_votes
    templates.env.filters["dt"] = _TemplateFilters.format_dt
    templates.env.filters["episode_label"] = _TemplateFilters.season_episode_label
    templates.env.filters["release_distance"] = _TemplateFilters.release_distance
    templates.env.filters["progress_effective_aired"] = progress_effective_aired
    templates.env.filters["progress_effective_percent"] = progress_effective_percent
    templates.env.filters["progress_skipped_count"] = progress_skipped_count
    templates.env.filters["progress_recent_release"] = progress_recent_release
    templates.env.filters["progress_rating_chip"] = lambda item: progress_rating_chip(item, _TemplateFilters.format_rating_with_votes)
    templates.env.filters["progress_episode_rating_chip"] = lambda item: progress_episode_rating_chip(item, _TemplateFilters.format_rating_with_votes)
    templates.env.filters["cached_image_url"] = lambda value: (f"/cached-image?url={quote(str(value))}&v=3" if value else "")
    templates.env.filters["episode_preview_url"] = tmdb_episode_preview_url
    return templates


def _results_need_enrichment(results: list) -> bool:
    for item in results:
        if (item.tmdb_id or item.imdb_id) and (
            not item.poster_url or item.tmdb_rating is None or item.imdb_rating is None
        ):
            return True
    return False


def _enrich_search_results(
    services: ServiceContainer,
    results: list,
    *,
    query: str,
    title_type: str | None,
    save_search_state: bool = True,
) -> tuple[list, bool]:
    if not results or not _results_need_enrichment(results):
        return results, False
    enriched_results = []
    for item in results:
        try:
            enriched_results.append(services.catalog.enrich_title_with_tmdb(item))
        except Exception:
            enriched_results.append(item)
    if query and save_search_state:
        services.catalog.save_last_search_state(query, title_type, enriched_results)
    return enriched_results, True


def _schedule_search_enrichment(
    app,
    *,
    results: list,
    query: str,
    title_type: str | None,
    task_key: str = "",
    source: str = "Search enrichment",
    save_search_state: bool = True,
) -> bool:
    if not results or not _results_need_enrichment(results):
        return False
    services: ServiceContainer = app.state.services
    bg_tasks = app.state.bg_tasks
    key = task_key or f"search_enrichment:{title_type or 'all'}:{query.strip().casefold()}"

    def run_enrichment() -> None:
        _enrich_search_results(
            services,
            list(results),
            query=query,
            title_type=title_type,
            save_search_state=save_search_state,
        )

    return bg_tasks.start(
        key,
        source=source,
        operations=services.operations,
        fn=run_enrichment,
    )


def create_app(
    config_store: ConfigStore | None = None,
    *,
    runtime: PortalRuntime | None = None,
) -> FastAPI:
    owns_runtime = runtime is None
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.imdb_auto_sync_loop.start()
        application.state.trakt_episode_ratings_refresh_loop.start()
        application.state.artwork_cache_warm_loop.start()
        try:
            yield
        finally:
            application.state.imdb_auto_sync_loop.stop()
            application.state.trakt_episode_ratings_refresh_loop.stop()
            application.state.artwork_cache_warm_loop.stop()
            if application.state.owns_runtime:
                application.state.runtime.close()

    app = FastAPI(title="Trakt Tracker Web Portal", lifespan=lifespan)
    app.state.runtime = runtime or _build_runtime_with_profiling(config_store)
    app.state.owns_runtime = owns_runtime
    app.state.services = app.state.runtime.services
    recover_interrupted_setup(app.state.services.database, task_running=False)
    _TemplateFilters.utc_offset = app.state.services.auth.config.utc_offset
    app.state.request_timing_log = get_app_data_dir() / "web_request_timings.log"
    app.state.image_cache = BinaryCache("images")
    app.state.bg_tasks = app.state.runtime.background_tasks
    app.state.imdb_auto_sync_loop = _IMDbAutoSyncLoop(app)
    app.state.trakt_episode_ratings_refresh_loop = _TraktEpisodeRatingsRefreshLoop(app)
    app.state.artwork_cache_warm_loop = _ArtworkCacheWarmLoop(app)

    templates = _build_templates()
    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    try:
        app.state.style_css_version = int((static_dir / "style.css").stat().st_mtime)
    except OSError:
        app.state.style_css_version = 0
    try:
        app.state.static_js_version = max(
            int(path.stat().st_mtime)
            for path in static_dir.glob("*.js")
        )
    except (OSError, ValueError):
        app.state.static_js_version = 0

    def reconnect_required(services: ServiceContainer, *, setup_complete: bool | None = None) -> bool:
        explicit = getattr(services.auth, "reauthorization_required", None)
        if callable(explicit) and explicit():
            return True
        if services.auth.is_authorized() or not services.auth.config.active_slug:
            return False
        if setup_complete is None:
            setup_complete = read_setup_state(services.database).get("state") == "complete"
        return bool(setup_complete)

    def reconnect_return_path(request: Request) -> str:
        if request.method == "GET":
            return request.url.path + (f"?{request.url.query}" if request.url.query else "")
        referer = str(request.headers.get("referer", "") or "").strip()
        if referer:
            parsed = urlsplit(referer)
            if parsed.hostname == request.url.hostname and parsed.path.startswith("/") and not parsed.path.startswith("//"):
                return parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return "/progress"

    def reconnect_response(request: Request, *, json_response: bool):
        message = "Trakt could not update your token automatically. Update it to continue."
        return_to = reconnect_return_path(request)
        if json_response:
            return JSONResponse(
                {
                    "detail": message,
                    "code": "trakt_reauth_required",
                    "return_to": return_to,
                },
                status_code=401,
            )
        return render(
            request,
            "reauthorization_required.html",
            {
                "page_title": "Update Trakt token",
                "flash": "",
                "reconnect_return_to": return_to,
                "suppress_auth_banner": True,
            },
            status_code=401,
        )

    @app.middleware("http")
    async def capture_request_timing(request: Request, call_next):
        started = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            log_line = (
                f"{datetime.now(tz=UTC).isoformat()} "
                f"{request.method} {request.url.path} status={status_code} elapsed_ms={elapsed_ms:.1f}\n"
            )
            log_path = request.app.state.request_timing_log
            _append_rotating_runtime_log(log_path, log_line)

    @app.middleware("http")
    async def require_completed_setup(request: Request, call_next):
        path = request.url.path
        allowed = (
            path == "/healthz"
            or path == "/setup"
            or path.startswith("/setup/")
            or path == "/settings"
            or path.startswith("/settings/")
            or path == "/cached-image"
            or path == "/notification-sound"
            or path.startswith("/static/")
        )
        if allowed:
            return await call_next(request)

        services: ServiceContainer = request.app.state.services
        authorized = services.auth.is_authorized()
        profile_setup_complete = read_setup_state(services.database).get("state") == "complete"
        accepts_json = "application/json" in request.headers.get("accept", "").casefold()
        sends_json = "application/json" in request.headers.get("content-type", "").casefold()
        partial_request = request.headers.get("x-trakt-partial", "").casefold() == "catalog"
        fetch_request = request.headers.get("x-trakt-fetch", "") == "1"
        json_response = (
            accepts_json
            or sends_json
            or partial_request
            or fetch_request
            or path.startswith("/notifications/")
        )
        if reconnect_required(services, setup_complete=profile_setup_complete):
            return reconnect_response(request, json_response=json_response)
        if authorized and profile_setup_complete:
            try:
                response = await call_next(request)
            except TraktReauthorizationRequired:
                return reconnect_response(request, json_response=json_response)
            if reconnect_required(services, setup_complete=True):
                return reconnect_response(request, json_response=json_response)
            return response

        if json_response:
            status_code = 401 if not authorized else 409
            return JSONResponse(
                {"detail": "Trakt authorization required" if not authorized else "Initial setup is incomplete"},
                status_code=status_code,
            )
        return RedirectResponse(url="/setup", status_code=303 if request.method != "GET" else 302)

    @app.middleware("http")
    async def secure_local_portal(request: Request, call_next):
        return await portal_security_middleware(request, call_next)

    @app.get("/healthz")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": app_version()})

    def render(request: Request, template_name: str, context: dict, status_code: int = 200) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        authorized = services.auth.is_authorized()
        profile_setup_complete = read_setup_state(services.database).get("state") == "complete"
        sound_path = Path(str(request.app.state.services.auth.config.notification_sound_path or "")).expanduser()
        notification_sound_url = ""
        if sound_path.exists() and sound_path.is_file():
            stamp = int(sound_path.stat().st_mtime)
            notification_sound_url = f"/notification-sound?v={stamp}"
        try:
            released_title_count = request.app.state.services.release_tracking.released_count()
            progress_waiting_title_count = request.app.state.services.release_tracking.progress_waiting_count()
        except Exception:
            released_title_count = 0
            progress_waiting_title_count = 0
        base_context = {
            "request": request,
            "current_path": request.url.path,
            "authorized": authorized,
            "reauthorization_required": reconnect_required(
                services,
                setup_complete=profile_setup_complete,
            ),
            "configured": services.auth.is_configured(),
            "settings_utc_offset": request.app.state.services.auth.config.utc_offset,
            "active_profile_slug": request.app.state.services.auth.config.active_slug,
            "web_hide_spoilers": bool(
                getattr(request.app.state.services.auth.config, "web_hide_spoilers", False)
            ),
            "notification_sound_url": notification_sound_url,
            "notifications_browser_poll_enabled": os.environ.get("TRAKT_TRACKER_TRAY_RUNTIME") != "1",
            "notification_activity_initial_seq": request.app.state.services.notifications.current_activity_seq(),
            "notification_pending_sources": ",".join(
                request.app.state.services.notifications.refresh_pending_sources()
            ),
            "debug_mode": request.app.state.services.auth.config.debug_mode,
            "debug_initial_seq": request.app.state.services.operations.current_seq(),
            "style_css_version": request.app.state.style_css_version,
            "static_js_version": request.app.state.static_js_version,
            "csrf_token": request.state.csrf_token,
            "released_title_count": released_title_count,
            "progress_waiting_title_count": progress_waiting_title_count,
        }
        base_context.update(context)
        return templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

    def render_fragment(request: Request, template_name: str, context: dict) -> str:
        config = request.app.state.services.auth.config
        fragment_context = {
            "request": request,
            "current_path": request.url.path,
            "active_profile_slug": config.active_slug,
            "web_hide_spoilers": bool(getattr(config, "web_hide_spoilers", False)),
        }
        fragment_context.update(context)
        return templates.get_template(template_name).render(fragment_context)

    app.state.render_fragment = render_fragment

    def progress_redirect(
        *,
        hide_upcoming: bool,
        show_paused: bool = False,
        show_dropped: bool,
        sort_mode: str = "episode_release",
        sort_direction: str = "desc",
        min_year: int | None = None,
        use_year_filter: bool = False,
        flash: str = "",
        rate_trakt_id: int | None = None,
        rate_season: int | None = None,
        rate_episode: int | None = None,
        rate_title: str = "",
    ) -> RedirectResponse:
        query = progress_query_string(
            hide_upcoming=hide_upcoming,
            show_paused=show_paused,
            show_dropped=show_dropped,
            sort_mode=sort_mode,
            sort_direction=sort_direction,
            min_year=min_year,
            use_year_filter=use_year_filter,
            flash=flash,
            rate_trakt_id=rate_trakt_id,
            rate_season=rate_season,
            rate_episode=rate_episode,
            rate_title=rate_title,
        )
        return RedirectResponse(url=f"/progress?{query}", status_code=303)

    register_system_routes(app, render=render, template_filters=_TemplateFilters)
    register_rating_routes(app)
    register_progress_routes(app, render=render, progress_redirect=progress_redirect)
    register_history_routes(app, render=render, render_fragment=render_fragment)
    register_catalog_routes(
        app,
        render=render,
        render_fragment=render_fragment,
        schedule_search_enrichment=_schedule_search_enrichment,
    )
    return app
