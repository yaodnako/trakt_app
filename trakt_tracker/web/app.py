from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trakt_tracker.application.services import ServiceContainer, build_services
from trakt_tracker.config import ConfigStore, format_local_datetime, get_app_data_dir
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.persistence.database import Database
from trakt_tracker.startup_profile import StartupProfiler
from trakt_tracker.web.routes_catalog import register_catalog_routes
from trakt_tracker.web.routes_history import register_history_routes
from trakt_tracker.web.routes_progress import register_progress_routes
from trakt_tracker.web.routes_system import register_system_routes
from trakt_tracker.web.viewmodels import (
    progress_effective_aired,
    progress_effective_percent,
    progress_query_string,
    progress_rating_chip,
    progress_recent_release,
    progress_skipped_count,
)


class _TemplateFilters:
    utc_offset = "+03:00"

    @staticmethod
    def format_compact_votes(value: int | None) -> str:
        if value is None:
            return ""
        if value < 1_000:
            return str(value)
        if value < 1_000_000:
            return f"{value / 1_000:.2f}".rstrip("0").rstrip(".") + "k"
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"

    @staticmethod
    def format_rating_with_votes(rating: float | None, votes: int | None) -> str:
        if rating is None:
            return "n/a"
        compact_votes = _TemplateFilters.format_compact_votes(votes)
        if compact_votes:
            return f"{rating:.1f} ({compact_votes})"
        return f"{rating:.1f}"

    @staticmethod
    def format_dt(value) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return format_local_datetime(value, _TemplateFilters.utc_offset)
        return str(value)

    @staticmethod
    def season_episode_label(season: int | None, episode: int | None) -> str:
        if season is None or episode is None:
            return ""
        return f"S{season:02d}E{episode:02d}"


class _BackgroundTaskManager:
    def __init__(self) -> None:
        self._running: set[str] = set()
        self._lock = Lock()

    def start(self, key: str, *, source: str, operations, fn) -> bool:
        with self._lock:
            if key in self._running:
                operations.publish(source, f"{source}: already running.")
                return False
            self._running.add(key)

        def runner() -> None:
            operations.publish(source, f"{source}: started.")
            try:
                fn()
            except Exception as exc:
                operations.publish(source, f"{source}: failed: {exc}")
            else:
                operations.publish(source, f"{source}: completed.")
            finally:
                with self._lock:
                    self._running.discard(key)

        Thread(target=runner, daemon=True).start()
        return True

    def is_running(self, key: str) -> bool:
        with self._lock:
            return key in self._running


def _build_services_with_profiling() -> ServiceContainer:
    profile_path = get_app_data_dir() / "web_startup.log"
    profiler = StartupProfiler(profile_path)
    config_store = ConfigStore()
    profiler.mark("config store ready")
    config = config_store.load()
    profiler.mark("config loaded")
    db = Database(config.resolved_database_path)
    profiler.mark("database opened")
    db.create_schema()
    profiler.mark("database schema ready")
    services = build_services(config_store, db)
    profiler.mark("services built")
    profiler.finish("web app ready")
    return services


def _build_templates() -> Jinja2Templates:
    templates_dir = Path(__file__).with_name("templates")
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["compact_votes"] = _TemplateFilters.format_compact_votes
    templates.env.filters["rating_with_votes"] = _TemplateFilters.format_rating_with_votes
    templates.env.filters["dt"] = _TemplateFilters.format_dt
    templates.env.filters["episode_label"] = _TemplateFilters.season_episode_label
    templates.env.filters["progress_effective_aired"] = progress_effective_aired
    templates.env.filters["progress_effective_percent"] = progress_effective_percent
    templates.env.filters["progress_skipped_count"] = progress_skipped_count
    templates.env.filters["progress_recent_release"] = progress_recent_release
    templates.env.filters["progress_rating_chip"] = lambda item: progress_rating_chip(item, _TemplateFilters.format_rating_with_votes)
    templates.env.filters["cached_image_url"] = lambda value: (f"/cached-image?url={quote(str(value))}" if value else "")
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
) -> tuple[list, bool]:
    if not results or not _results_need_enrichment(results):
        return results, False
    enriched_results = []
    for item in results:
        try:
            enriched_results.append(services.catalog.enrich_title_with_tmdb(item))
        except Exception:
            enriched_results.append(item)
    if query:
        services.catalog.save_last_search_state(query, title_type, enriched_results)
    return enriched_results, True


def _schedule_search_enrichment(app, *, results: list, query: str, title_type: str | None) -> bool:
    if not results or not _results_need_enrichment(results):
        return False
    services: ServiceContainer = app.state.services
    bg_tasks = app.state.bg_tasks
    key = f"search_enrichment:{title_type or 'all'}:{query.strip().casefold()}"

    def run_enrichment() -> None:
        _enrich_search_results(
            services,
            list(results),
            query=query,
            title_type=title_type,
        )

    return bg_tasks.start(
        key,
        source="Search enrichment",
        operations=services.operations,
        fn=run_enrichment,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Trakt Tracker Web Prototype")
    app.state.services = _build_services_with_profiling()
    _TemplateFilters.utc_offset = app.state.services.auth.config.utc_offset
    app.state.request_timing_log = get_app_data_dir() / "web_request_timings.log"
    app.state.image_cache = BinaryCache("images")
    app.state.bg_tasks = _BackgroundTaskManager()

    templates = _build_templates()
    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(log_line)
            except OSError:
                pass
            if (
                request.url.path not in {"/cached-image", "/debug/events"}
                and not request.url.path.startswith("/static")
            ):
                services: ServiceContainer = request.app.state.services
                bg_tasks = request.app.state.bg_tasks
                interval_hours = max(1, int(services.auth.config.imdb_auto_sync_interval_hours or 1))
                if services.sync.should_auto_sync_imdb_dataset(interval_hours):
                    bg_tasks.start(
                        "imdb_auto_sync",
                        source="IMDb sync (auto)",
                        operations=services.operations,
                        fn=lambda: services.sync.maybe_sync_imdb_dataset(
                            interval_hours,
                            status_callback=lambda message: services.operations.publish("IMDb sync", message),
                        ),
                    )

    def render(request: Request, template_name: str, context: dict, status_code: int = 200) -> HTMLResponse:
        base_context = {
            "request": request,
            "current_path": request.url.path,
            "authorized": request.app.state.services.auth.is_authorized(),
            "configured": request.app.state.services.auth.is_configured(),
            "settings_utc_offset": request.app.state.services.auth.config.utc_offset,
            "debug_mode": request.app.state.services.auth.config.debug_mode,
            "debug_initial_seq": request.app.state.services.operations.current_seq(),
        }
        base_context.update(context)
        return templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

    def progress_redirect(
        *,
        hide_upcoming: bool,
        show_dropped: bool,
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
            show_dropped=show_dropped,
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
    register_progress_routes(app, render=render, progress_redirect=progress_redirect)
    register_history_routes(app, render=render)
    register_catalog_routes(app, render=render, enrich_search_results=_enrich_search_results, schedule_search_enrichment=_schedule_search_enrichment)
    return app


app = create_app()
