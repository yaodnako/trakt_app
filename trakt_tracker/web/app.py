from __future__ import annotations

from datetime import UTC, datetime
import mimetypes
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trakt_tracker.application.services import ServiceContainer, build_services
from trakt_tracker.config import ConfigStore, format_local_datetime, get_app_data_dir, normalize_utc_offset
from trakt_tracker.domain import RatingInput
from trakt_tracker.persistence.database import Database
from trakt_tracker.startup_profile import StartupProfiler
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.web.viewmodels import (
    DEFAULT_SEARCH_SORT_MODE,
    HISTORY_PAGE_SIZE,
    SEARCH_SORT_MODES,
    filter_progress_items,
    normalize_search_sort_mode,
    normalize_title_type,
    parse_bool_flag,
    parse_progress_year,
    progress_effective_aired,
    progress_effective_percent,
    progress_query_string,
    progress_recent_release,
    progress_rating_chip,
    progress_skipped_count,
    saved_search_matches,
    sort_search_results,
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


def _image_cache_suffix(url: str, content_type: str | None = None) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type:
        guessed = mimetypes.guess_extension(media_type)
        if guessed:
            return guessed
    guessed_from_url, _ = mimetypes.guess_type(url)
    if guessed_from_url:
        guessed = mimetypes.guess_extension(guessed_from_url)
        if guessed:
            return guessed
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix
    return ".img"


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
                cache.set_bytes(target_url, fetched, suffix=_image_cache_suffix(target_url, content_type))
                return Response(content=fetched, media_type=resolved_media_type)
        except Exception:
            pass
        return RedirectResponse(url=target_url, status_code=307)

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
                "flash": flash,
                "rate_trakt_id": rate_trakt_id,
                "rate_season": rate_season,
                "rate_episode": rate_episode,
                "rate_title": rate_title,
            },
        )

    @app.post("/progress/sync")
    async def progress_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks: _BackgroundTaskManager = request.app.state.bg_tasks
        form = await request.form()
        hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
        show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
        min_year_value = parse_progress_year(str(form.get("min_year", "")))
        use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
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
        items = services.progress.dashboard_progress(dropped_only=show_dropped_value)
        current = next((item for item in items if item.trakt_id == trakt_id), None)
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
        hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
        show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
        min_year_value = parse_progress_year(str(form.get("min_year", "")))
        use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
        items = services.progress.dashboard_progress(dropped_only=show_dropped_value)
        current = next((item for item in items if item.trakt_id == trakt_id), None)
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
        hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
        show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
        min_year_value = parse_progress_year(str(form.get("min_year", "")))
        use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
        items = services.progress.dashboard_progress(dropped_only=show_dropped_value)
        current = next((item for item in items if item.trakt_id == trakt_id), None)
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
        hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
        show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
        min_year_value = parse_progress_year(str(form.get("min_year", "")))
        use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
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
        hide_upcoming_value = parse_bool_flag(str(form.get("hide_upcoming", "")))
        show_dropped_value = parse_bool_flag(str(form.get("show_dropped", "")))
        min_year_value = parse_progress_year(str(form.get("min_year", "")))
        use_year_filter_value = parse_bool_flag(str(form.get("use_year_filter", "")))
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

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(
        request: Request,
        type: str = "all",
        title: str = "",
        page: int = 1,
        flash: str = "",
        rate_trakt_id: int | None = None,
        rate_type: str = "",
        rate_season: int | None = None,
        rate_episode: int | None = None,
        rate_title: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        title_type = normalize_title_type(type)
        current_page = max(1, page)
        title_filter = title.strip() or None
        rows = services.history.history(
            title_type=title_type,
            title_filter=title_filter,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=(current_page - 1) * HISTORY_PAGE_SIZE,
        )
        has_next = len(rows) > HISTORY_PAGE_SIZE
        rows = rows[:HISTORY_PAGE_SIZE]
        title_options = services.history.history_titles(title_type=title_type)
        return render(
            request,
            "history.html",
            {
                "page_title": "History",
                "history_rows": rows,
                "history_type": title_type or "all",
                "history_title_filter": title.strip(),
                "history_title_options": title_options,
                "page": current_page,
                "has_next": has_next,
                "rate_trakt_id": rate_trakt_id,
                "rate_type": normalize_title_type(rate_type) or "",
                "rate_season": rate_season,
                "rate_episode": rate_episode,
                "rate_title": rate_title,
                "flash": flash,
            },
                )

    @app.get("/history/auto-sync")
    async def history_auto_sync(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            changed = services.sync.maybe_refresh_history()
        except Exception as exc:
            return JSONResponse({"changed": False, "error": str(exc), "message": f"History auto-sync failed: {exc}"})
        return JSONResponse(
            {
                "changed": bool(changed),
                "message": "History auto-sync updated rows." if changed else "History auto-sync: no changes.",
            }
        )

    @app.post("/history/sync")
    async def history_sync(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        bg_tasks: _BackgroundTaskManager = request.app.state.bg_tasks
        form = await request.form()
        history_type = normalize_title_type(str(form.get("type", "all") or "all")) or "all"
        title_filter = str(form.get("title_filter", "") or "")
        try:
            page = max(1, int(str(form.get("page", "1") or "1")))
        except ValueError:
            page = 1
        started = bg_tasks.start(
            "history_sync",
            source="History sync (manual full)",
            operations=services.operations,
            fn=services.sync.refresh_history,
        )
        flash = "History sync started." if started else "History sync is already running."
        redirect_url = f"/history?type={history_type}&title={quote(title_filter)}&page={page}&flash={quote(flash)}"
        return RedirectResponse(url=redirect_url, status_code=303)

    @app.post("/history/rate")
    async def history_rate(request: Request) -> RedirectResponse:
        services: ServiceContainer = request.app.state.services
        form = await request.form()
        history_type = normalize_title_type(str(form.get("type", "all") or "all")) or "all"
        title_filter = str(form.get("title_filter", "") or "")
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
        redirect_url = f"/history?type={history_type}&title={quote(title_filter)}&page={page}&flash={quote(flash)}"
        return RedirectResponse(url=redirect_url, status_code=303)

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
        config.cache_ttl_hours = max(1, min(168, cache_ttl_hours))
        config.poll_interval_minutes = max(5, min(240, poll_interval_minutes))
        config.notifications_enabled = parse_bool_flag(str(form.get("notifications_enabled", "")))
        config.debug_mode = parse_bool_flag(str(form.get("debug_mode", "")))
        config.open_in_embedded_player = parse_bool_flag(str(form.get("open_in_embedded_player", "")))
        config.utc_offset = normalize_utc_offset(str(form.get("utc_offset", config.utc_offset or "+03:00")))
        ConfigStore().save(config)
        _TemplateFilters.utc_offset = config.utc_offset
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

    @app.get("/search", response_class=HTMLResponse)
    async def search_page(
        request: Request,
        q: str = "",
        type: str = "all",
        sort: str = "",
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        saved_state = services.catalog.load_last_search_state()
        title_type = normalize_title_type(type)
        query = q.strip()
        sort_mode = normalize_search_sort_mode(sort, services.catalog.get_search_sort_mode())
        services.catalog.set_search_sort_mode(sort_mode)

        results = []
        source_label = ""
        error_message = ""
        selected_type = title_type or "all"

        if query:
            try:
                if saved_search_matches(saved_state, query, title_type):
                    results = list(saved_state.get("results", []))
                    source_label = "Local cached result set"
                else:
                    results = services.catalog.search_titles(query, title_type)
                    source_label = "Fresh Trakt search"
                results, enriched = _enrich_search_results(
                    services,
                    results,
                    query=query,
                    title_type=title_type,
                )
                if enriched:
                    source_label += " with metadata enrichment"
            except Exception as exc:
                error_message = str(exc)
        elif saved_state:
            results = list(saved_state.get("results", []))
            query = str(saved_state.get("query", "") or "").strip()
            selected_type = normalize_title_type(saved_state.get("title_type")) or "all"
            source_label = "Last saved search"
            results, enriched = _enrich_search_results(
                services,
                results,
                query=query,
                title_type=normalize_title_type(saved_state.get("title_type")),
            )
            if enriched:
                source_label += " with metadata enrichment"

        results = sort_search_results(results, sort_mode)
        return render(
            request,
            "search.html",
            {
                "page_title": "Search",
                "query": query,
                "search_type": selected_type,
                "sort_mode": sort_mode or DEFAULT_SEARCH_SORT_MODE,
                "sort_modes": SEARCH_SORT_MODES,
                "results": results,
                "search_history": services.catalog.search_history(),
                "source_label": source_label,
                "error_message": error_message,
            },
        )

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
            title_item = services.catalog.get_title_details(trakt_id, normalized_type)
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

    return app


app = create_app()
