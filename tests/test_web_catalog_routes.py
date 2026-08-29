from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trakt_tracker.application.episode_ratings_matrix import (
    EpisodeMatrixCell,
    EpisodeMatrixLegendItem,
    EpisodeMatrixRow,
    EpisodeMatrixSeason,
    EpisodeRatingsMatrixViewModel,
    rating_bucket_color,
)
from trakt_tracker.application.enrich_state import ENRICH_STATUS_CHECKED_NO_DATA
from trakt_tracker.web.routes_catalog import register_catalog_routes
from trakt_tracker.domain import ExploreResultPage, TitleSummary
from trakt_tracker.application.search_watch import SearchShowWatchPanel, SearchWatchEpisode, SearchWatchSeason
from trakt_tracker.application.tmdb_catalog import TmdbCatalogItem, TmdbCatalogPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "trakt_tracker" / "web" / "static"


class _FakeEpisodeRatingsMatrixService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.imdb_layout_available = True

    def load_show_matrix(
        self,
        trakt_id: int,
        *,
        force_refresh: bool = False,
        provider: str = "imdb",
        refresh_missing: bool = False,
        allow_network_refresh: bool = True,
    ) -> EpisodeRatingsMatrixViewModel:
        self.calls.append(
            {
                "trakt_id": trakt_id,
                "force_refresh": force_refresh,
                "provider": provider,
                "refresh_missing": refresh_missing,
                "allow_network_refresh": allow_network_refresh,
            }
        )

        season = EpisodeMatrixSeason(season=1, label="S1", avg_display="8.3", avg_rating=8.3, avg_color="rgb(40, 180, 99)")
        overall = EpisodeMatrixSeason(season=-1, label="ALL", avg_display="8.3", avg_rating=8.3, avg_color="rgb(40, 180, 99)")
        row = EpisodeMatrixRow(
            episode=1,
            label="E1",
            cells=[
                EpisodeMatrixCell(
                    season=1,
                    episode=1,
                    exists=True,
                    display_value="8.3",
                    imdb_rating=8.3,
                    color="rgb(40, 180, 99)",
                    state="rated",
                    title="Pilot",
                )
            ],
        )
        return EpisodeRatingsMatrixViewModel(
            trakt_id=trakt_id,
            title="The Capture",
            subtitle=("Trakt episode ratings by season" if provider == "trakt" else "IMDb episode ratings by season"),
            title_trakt_rating=8.2,
            title_trakt_votes=1200,
            title_imdb_rating=8.4,
            title_imdb_votes=3400,
            title_ratings_status="ready",
            legend=[EpisodeMatrixLegendItem(label="Awesome", threshold_label=">= 9", color="rgb(24, 106, 59)")],
            seasons=[season, overall],
            rows=[row],
            imdb_seasons=[season, overall],
            imdb_rows=[row],
            imdb_layout_available=self.imdb_layout_available,
            has_episodes=True,
            provider=("trakt" if provider == "trakt" else "imdb"),
        )

    def select_trakt_rating_refresh_keys(self, trakt_id: int, *, force_refresh: bool = False) -> list[tuple[int, int]]:
        return [(1, 1)]

    def load_imdb_show_matrix(
        self,
        *,
        title: str,
        imdb_id: str,
        title_tmdb_rating: float | None = None,
        title_tmdb_votes: int | None = None,
        title_imdb_rating: float | None = None,
        title_imdb_votes: int | None = None,
        title_ratings_status: str = "",
        my_ratings: dict[tuple[int, int], int] | None = None,
    ) -> EpisodeRatingsMatrixViewModel:
        matrix = self.load_show_matrix(0, allow_network_refresh=False)
        matrix.title = title
        matrix.title_trakt_rating = None
        matrix.title_trakt_votes = None
        matrix.title_imdb_rating = title_imdb_rating
        matrix.title_imdb_votes = title_imdb_votes
        matrix.title_ratings_status = title_ratings_status
        matrix.title_primary_provider = "tmdb"
        matrix.title_primary_rating = title_tmdb_rating
        matrix.title_primary_votes = title_tmdb_votes
        self.calls.append({"imdb_id": imdb_id, "title": title, "my_ratings": my_ratings or {}})
        return matrix


class _FakeSearchWatchService:
    def __init__(self) -> None:
        self.load_calls: list[dict] = []
        self.mark_calls: list[dict] = []
        self.enrich_still_calls: list[tuple[int, int]] = []
        self.return_still_after_enrich = False
        self.pending_still_before_enrich = False
        self.episodes_hydrated = True
        self.hydrate_result = True
        self.hydrate_calls: list[int] = []
        self.unmark_calls: list[dict] = []
        self.restore_calls: list[dict] = []
        self.unmark_scope_calls: list[dict] = []
        self.restore_scope_calls: list[list[dict]] = []
        self.mapping_pending = False
        self.imdb_layout_available = True
        self.repair_calls: list[int] = []

    def load_show_panel(
        self,
        trakt_id: int,
        default_season: int | None = None,
        *,
        season_layout: str = "trakt",
    ) -> SearchShowWatchPanel:
        self.load_calls.append(
            {
                "trakt_id": trakt_id,
                "default_season": default_season,
                "season_layout": season_layout,
            }
        )
        if not self.episodes_hydrated:
            return SearchShowWatchPanel(
                trakt_id=trakt_id,
                title="The Capture",
                slug="the-capture",
                seasons=[],
                season_layout=season_layout,
            )
        effective_layout = season_layout if self.imdb_layout_available else "trakt"
        selected_season = 1 if default_season is None else default_season
        return SearchShowWatchPanel(
            trakt_id=trakt_id,
            title="The Capture",
            slug="the-capture",
            poster_url="https://poster.example/capture.jpg",
            title_trakt_rating=8.2,
            title_trakt_votes=1200,
            title_imdb_rating=8.4,
            title_imdb_votes=3400,
            title_ratings_status="ready",
            seasons=[
                SearchWatchSeason(
                    season=0,
                    label="S0",
                    is_default=selected_season == 0,
                    bulk_allowed=effective_layout == "trakt",
                    episodes=[
                        SearchWatchEpisode(
                            season=0,
                            number=1,
                            title="Special",
                            first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                        )
                    ],
                ),
                SearchWatchSeason(
                    season=1,
                    label="S1",
                    is_default=selected_season == 1,
                    bulk_allowed=not (effective_layout == "imdb" and self.mapping_pending),
                    episodes=[
                        SearchWatchEpisode(
                            season=1,
                            number=1,
                            imdb_season=2,
                            imdb_episode=1,
                            title="Pilot",
                            still_url="https://still.example/pilot.jpg",
                            trakt_rating=8.1,
                            trakt_votes=100,
                            imdb_rating=8.3,
                            imdb_votes=120,
                            first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                            is_watched=True,
                            user_rating=8,
                        ),
                        SearchWatchEpisode(
                            season=1,
                            number=2,
                            title="No Still",
                            still_url=(
                                "https://still.example/no-still.jpg"
                                if self.return_still_after_enrich and self.enrich_still_calls
                                else ""
                            ),
                            still_status=(
                                "ready"
                                if self.return_still_after_enrich and self.enrich_still_calls
                                else (
                                    "unknown"
                                    if self.pending_still_before_enrich
                                    else "checked_no_data"
                                )
                            ),
                            first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                            is_watched=True,
                        )
                    ],
                ),
            ],
            season_layout=effective_layout,
            imdb_layout_available=self.imdb_layout_available,
            imdb_mapping_complete=not self.mapping_pending,
            imdb_mapping_pending=self.mapping_pending,
        )

    def mark_watch(self, **kwargs) -> int:
        self.mark_calls.append(kwargs)
        return 3 if kwargs.get("scope") == "title" else 1

    def unmark_episode(self, **kwargs) -> dict:
        self.unmark_calls.append(kwargs)
        return {
            "title_type": "show",
            "trakt_id": kwargs["trakt_id"],
            "title": "The Capture",
            "season": kwargs["season"],
            "episode": kwargs["episode"],
            "watched_at": "2026-07-01T12:00:00+00:00",
            "watched_at_known": True,
        }

    def restore_episode(self, **kwargs) -> None:
        self.restore_calls.append(kwargs)

    def unmark_scope(self, **kwargs) -> dict:
        self.unmark_scope_calls.append(kwargs)
        return {
            "kind": "scope",
            "title_type": kwargs["title_type"],
            "trakt_id": kwargs["trakt_id"],
            "title": "Movie A",
            "scope": kwargs["scope"],
            "season": kwargs.get("season"),
            "still_watched": False,
            "items": [
                {
                    "title_type": kwargs["title_type"],
                    "trakt_id": kwargs["trakt_id"],
                    "title": "Movie A",
                    "season": kwargs.get("season"),
                    "episode": None,
                    "watched_at": "2026-07-01T12:00:00+00:00",
                    "watched_at_known": True,
                }
            ],
        }

    def restore_scope(self, *, items: list[dict]) -> None:
        self.restore_scope_calls.append(items)

    def enrich_missing_stills(self, trakt_id: int, season: int) -> bool:
        self.enrich_still_calls.append((trakt_id, season))
        return True

    def hydrate_show_episodes(self, trakt_id: int) -> bool:
        self.hydrate_calls.append(trakt_id)
        if self.hydrate_result:
            self.episodes_hydrated = True
        return self.hydrate_result

    def repair_imdb_seasons(self, trakt_id: int) -> int:
        self.repair_calls.append(trakt_id)
        return 1


class CatalogRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        static_dir = STATIC_DIR
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["rating_with_votes"] = lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a"
        self.templates.env.filters["compact_votes"] = lambda value: f"{value / 1000:.1f}k" if value >= 1000 else str(value)
        self.templates.env.filters["cached_image_url"] = lambda value: (f"/cached-image?url={quote(str(value))}&v=3" if value else "")
        self.templates.env.filters["rating_bucket_color"] = rating_bucket_color
        self.templates.env.filters["episode_preview_url"] = lambda value: str(value).replace("/w780/", "/w342/")
        self.templates.env.filters["dt"] = lambda value: value.isoformat() if value else ""
        self.templates.env.filters["release_distance"] = lambda value: "soon" if value else "Release date unknown"
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.matrix = _FakeEpisodeRatingsMatrixService()
        self.search_watch = _FakeSearchWatchService()
        self.watchlist_calls: list[dict] = []
        self.catalog_watchlist_keys = {("show", 3)}
        self.release_tracking_keys = set()
        self.release_tracking_calls: list[dict] = []
        self.catalog_history_keys = {("movie", 1)}
        self.enrichment_calls: list[dict] = []
        self.enrich_tasks: list[object] = []
        self.image_tasks: list[tuple[str, int]] = []
        self.progress_refresh_calls: list[tuple[int, bool, bool]] = []
        self.bg_task_calls: list[tuple[tuple, dict]] = []
        self.explore_saved_filters = {"imdb_min": "", "trakt_min": ""}
        self.search_saved_filters = {"imdb_min": "", "trakt_min": ""}
        watchlist_title = TitleSummary(
            trakt_id=3,
            title_type="show",
            title="The Capture",
            poster_url="https://poster.example/capture.jpg",
            trakt_rating=8.0,
            trakt_votes=80,
            is_watchlisted=True,
            watchlisted_at=datetime(2026, 7, 1, tzinfo=UTC),
            released_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        watchlist_movie = TitleSummary(
            trakt_id=4,
            title_type="movie",
            title="Arrival",
            is_watchlisted=True,
            watchlisted_at=datetime(2026, 6, 1, tzinfo=UTC),
            released_at=datetime(2016, 9, 1, tzinfo=UTC),
            imdb_rating=7.9,
        )

        def explore_titles(title_type, feed, *, page, limit):
            is_future = feed == "anticipated"
            item = TitleSummary(
                trakt_id=5,
                title_type=title_type,
                title="Future Show" if title_type == "show" else "Current Movie",
                slug="future-show" if title_type == "show" else "current-movie",
                poster_url="https://poster.example/explore.jpg",
                released_at=datetime(2027, 1, 1, tzinfo=UTC) if is_future else datetime(2025, 1, 1, tzinfo=UTC),
                trakt_rating=7.4,
                imdb_rating=8.2,
                explore_metric_kind="lists" if feed == "anticipated" else ("watching" if feed == "trending" else ""),
                explore_metric_count=1200 if feed != "popular" else None,
                catalog_actions_available=not is_future,
            )
            return ExploreResultPage(items=[item], page=page, page_count=3)

        def filtered_explore_titles(title_type, feed, *, page, limit, imdb_min, trakt_min, max_scan_pages, excluded_keys=None):
            result = explore_titles(title_type, feed, page=page, limit=limit)
            if imdb_min is not None:
                result.items = [item for item in result.items if item.imdb_rating is not None and item.imdb_rating >= imdb_min]
            if trakt_min is not None:
                result.items = [item for item in result.items if item.trakt_rating is not None and item.trakt_rating >= trakt_min]
            result.items = [item for item in result.items if (item.title_type, item.trakt_id) not in (excluded_keys or set())]
            return result

        def save_explore_rating_filters(imdb_min, trakt_min, **kwargs):
            self.explore_saved_filters = {"imdb_min": imdb_min, "trakt_min": trakt_min, **kwargs}

        search_results = [
            TitleSummary(
                trakt_id=1,
                title_type="movie",
                title="Movie A",
                poster_url="https://poster.example/movie-a.jpg",
                trakt_rating=7.0,
                trakt_votes=10,
                ratings_status=ENRICH_STATUS_CHECKED_NO_DATA,
            ),
            TitleSummary(
                trakt_id=2,
                title_type="movie",
                title="Movie B",
                trakt_rating=6.0,
                trakt_votes=8,
                released_at=datetime(2027, 1, 1, tzinfo=UTC),
            ),
            TitleSummary(
                trakt_id=3,
                title_type="show",
                title="The Capture",
                poster_url="https://poster.example/capture.jpg",
                trakt_rating=8.0,
                trakt_votes=80,
                imdb_rating=8.3,
            ),
        ]
        self.release_items = [
            TitleSummary(
                trakt_id=21,
                title_type="show",
                title="Released Show",
                slug="released-show",
                poster_url="https://poster.example/released-show.jpg",
                released_at=datetime.now(tz=UTC) - timedelta(days=2),
                explore_metric_count=2300,
                trakt_rating=8.6,
                trakt_votes=860,
                imdb_rating=8.2,
                imdb_votes=12400,
            ),
            TitleSummary(
                trakt_id=22,
                title_type="show",
                title="Upcoming Show",
                slug="upcoming-show",
                released_at=datetime.now(tz=UTC) + timedelta(days=30),
                explore_metric_count=1200,
            ),
        ]

        def filtered_search_titles(query, title_type=None, *, page, limit, imdb_min, trakt_min, max_scan_pages, excluded_keys=None):
            items = [item for item in search_results if title_type is None or item.title_type == title_type]
            if imdb_min is not None:
                items = [item for item in items if item.imdb_rating is not None and item.imdb_rating >= imdb_min]
            if trakt_min is not None:
                items = [item for item in items if item.trakt_rating is not None and item.trakt_rating >= trakt_min]
            items = [item for item in items if (item.title_type, item.trakt_id) not in (excluded_keys or set())]
            return ExploreResultPage(items=items, page=page, page_count=page)

        def save_search_rating_filters(imdb_min, trakt_min, **kwargs):
            self.search_saved_filters = {"imdb_min": imdb_min, "trakt_min": trakt_min, **kwargs}
        self.app.state.services = SimpleNamespace(
            catalog=SimpleNamespace(
                load_last_search_state=lambda: None,
                save_last_search_state=lambda query, title_type, results, **kwargs: None,
                get_search_sort_mode=lambda: "IMDb votes",
                set_search_sort_mode=lambda value: value,
                search_history=lambda: [],
                watchlist_keys=lambda *, title_type=None: {
                    key for key in self.catalog_watchlist_keys if title_type is None or key[0] == title_type
                },
                history_keys=lambda: set(self.catalog_history_keys),
                local_watchlist_titles=lambda: [watchlist_title, watchlist_movie],
                watchlist_titles=lambda: [watchlist_title, watchlist_movie],
                explore_titles=explore_titles,
                filtered_explore_titles=filtered_explore_titles,
                local_explore_titles=lambda *args, **kwargs: None,
                refresh_explore_titles=filtered_explore_titles,
                load_explore_rating_filters=lambda: dict(self.explore_saved_filters),
                save_explore_rating_filters=save_explore_rating_filters,
                filtered_search_titles=filtered_search_titles,
                load_search_rating_filters=lambda: dict(self.search_saved_filters),
                save_search_rating_filters=save_search_rating_filters,
                set_watchlisted=lambda title_type, trakt_id, *, watchlisted, snapshot=None: self.watchlist_calls.append(
                    {"title_type": title_type, "trakt_id": trakt_id, "watchlisted": watchlisted}
                ),
                get_title_details=lambda trakt_id, title_type: TitleSummary(trakt_id=trakt_id, title_type=title_type, title="Fallback"),
                search_titles=lambda query, title_type=None: list(search_results),
            ),
            episode_ratings_matrix=self.matrix,
            search_watch=self.search_watch,
            enrich_queue=SimpleNamespace(
                submit=lambda task: self.enrich_tasks.append(task),
                submit_history_refresh=lambda **kwargs: self.enrich_tasks.extend(kwargs["viewport_tasks"]),
            ),
            image_queue=SimpleNamespace(
                submit_many=lambda urls, *, priority: self.image_tasks.extend((url, priority) for url in urls),
            ),
            history=SimpleNamespace(title_rating_badges=lambda trakt_ids: {1: 9.0, 3: 8.5}),
            progress=SimpleNamespace(
                refresh_show_progress=lambda trakt_id, *, fresh=False, enrich_assets=True: self.progress_refresh_calls.append(
                    (trakt_id, fresh, enrich_assets)
                )
            ),
            play=SimpleNamespace(resolve_kinopoisk_url=lambda title: f"https://kino.example/{quote(title)}" if title else None),
            operations=SimpleNamespace(publish=lambda *_args, **_kwargs: None),
            release_tracking=SimpleNamespace(
                keys=lambda: set(self.release_tracking_keys),
                local_keys=lambda: set(self.release_tracking_keys),
                local_items=lambda: list(self.release_items),
                matured_release_keys=lambda: {("show", 21)},
                notified_release_keys=lambda: {("show", 21)},
                refresh=lambda: list(self.release_items),
                refresh_anticipated_list_counts=lambda _items: None,
                set_tracked=lambda title_type, trakt_id, *, tracked: self.release_tracking_calls.append(
                    {
                        "title_type": title_type,
                        "trakt_id": trakt_id,
                        "tracked": tracked,
                    }
                ),
            ),
            auth=SimpleNamespace(
                config=SimpleNamespace(
                    catalog_provider_mode="trakt",
                    utc_offset="+03:00",
                    explore_imdb_scan_page_limit=10,
                    web_imdb_seasons_enabled=True,
                    web_hide_spoilers=False,
                    active_slug="test-user",
                )
            ),
            tmdb_catalog=SimpleNamespace(
                is_configured=lambda: True,
                search_titles=lambda query, title_type=None, *, page, limit: TmdbCatalogPage(
                    items=[
                        TmdbCatalogItem(
                            title_type="movie",
                            tmdb_id=603,
                            title="The Matrix",
                            year=1999,
                            poster_url="https://poster.example/matrix.jpg",
                            tmdb_rating=8.2,
                            tmdb_votes=28000,
                            imdb_id="tt0133093",
                            imdb_rating=8.7,
                            imdb_votes=2_100_000,
                            ratings_status="ready",
                        )
                    ],
                    page=page,
                    page_count=1,
                ),
                explore_titles=lambda title_type, feed, *, page, limit: TmdbCatalogPage(
                    items=[
                        TmdbCatalogItem(
                            title_type=title_type,
                            tmdb_id=101,
                            title="TMDb Explore Title",
                            year=2026,
                            poster_url="https://poster.example/tmdb-explore.jpg",
                            tmdb_rating=7.8,
                            tmdb_votes=12000,
                            imdb_id="tt0101",
                            imdb_rating=7.6,
                            imdb_votes=14000,
                            ratings_status="ready",
                            popularity=94.5,
                            explore_metric_kind="weekly trend" if feed == "trending" else "popularity",
                            explore_rank=1 if feed == "trending" else None,
                            released_at=datetime(2026, 7, 1, tzinfo=UTC),
                        )
                    ],
                    page=page,
                    page_count=2,
                ),
                local_release_items=lambda: [
                    TmdbCatalogItem(
                        title_type="movie",
                        tmdb_id=202,
                        title="TMDb Released Movie",
                        year=2026,
                        poster_url="https://poster.example/tmdb-release.jpg",
                        tmdb_rating=7.5,
                        tmdb_votes=9000,
                        imdb_id="tt0202",
                        imdb_rating=7.3,
                        imdb_votes=11000,
                        ratings_status="ready",
                        released_at=datetime.now(tz=UTC) - timedelta(days=2),
                        is_release_tracked=True,
                        is_notification_matured=False,
                    )
                ],
                refresh_release_items=lambda: [],
                notified_release_keys=lambda: {("movie", 202)},
                local_show_episode_ratings=lambda _tmdb_id: {},
            ),
        )
        self.saved_imdb_seasons: list[bool] = []
        self.config_store = SimpleNamespace(
            save=lambda config: self.saved_imdb_seasons.append(bool(config.web_imdb_seasons_enabled))
        )
        self.app.state.runtime = SimpleNamespace(config_store=self.config_store)
        def start_background_task(*args, **kwargs):
            self.bg_task_calls.append((args, kwargs))
            return True

        self.app.state.bg_tasks = SimpleNamespace(
            start=start_background_task,
            start_coalesced=start_background_task,
        )

        def render(request: Request, template_name: str, context: dict, status_code: int = 200) -> HTMLResponse:
            base_context = {
                "request": request,
                "current_path": request.url.path,
                "authorized": True,
                "configured": True,
                "settings_utc_offset": "+03:00",
                "notification_sound_url": "",
                "debug_mode": False,
                "debug_initial_seq": 0,
                "active_profile_slug": self.app.state.services.auth.config.active_slug,
                "web_hide_spoilers": self.app.state.services.auth.config.web_hide_spoilers,
            }
            base_context.update(context)
            return self.templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

        def render_fragment(request: Request, template_name: str, context: dict) -> str:
            fragment_context = {
                "request": request,
                "current_path": request.url.path,
                "active_profile_slug": self.app.state.services.auth.config.active_slug,
                "web_hide_spoilers": self.app.state.services.auth.config.web_hide_spoilers,
            }
            fragment_context.update(context)
            return self.templates.get_template(template_name).render(fragment_context)

        register_catalog_routes(
            self.app,
            render=render,
            render_fragment=render_fragment,
            schedule_search_enrichment=lambda *args, **kwargs: self.enrichment_calls.append(kwargs) or False,
        )
        self.client = TestClient(self.app)

    def test_show_matrix_fragment_route_renders_legend_and_grid(self) -> None:
        response = self.client.get("/titles/show/138748/episode-ratings-matrix")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Awesome", html)
        self.assertIn("S1", html)
        self.assertIn("E1", html)
        self.assertIn("AVG.", html)
        self.assertIn('data-title-matrix-provider-toggle', html)
        self.assertIn('data-title-matrix-provider="imdb"', html)
        self.assertIn('data-title-matrix-provider="trakt"', html)
        self.assertIn('data-title-matrix-rating-mode="my"', html)
        self.assertIn("imdb_icon.png", html)
        self.assertIn("trakt_logo_bw.svg", html)
        self.assertIn("My ratings", html)
        self.assertIn('data-imdb-seasons-toggle checked', html)
        self.assertIn('data-matrix-layout-panel="imdb"', html)
        self.assertIn('data-matrix-layout-panel="trakt" hidden', html)
        self.assertIn('data-title-trakt-rating="8.2 (1200)"', html)
        self.assertIn('data-title-imdb-rating="8.4 (3400)"', html)
        self.assertNotIn("title-matrix-imdb-coordinate", html)
        self.assertNotIn('data-my-rating-toggle', html)

    def test_tmdb_preview_search_reuses_existing_toolbar_and_card_layout(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        self.app.state.services.catalog.search_history = lambda: ["The Matrix"]

        response = self.client.get("/search?q=The+Matrix&type=all&sort=TMDb+votes&imdb_min=7&tmdb_min=8")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("search-toolbar-panel", html)
        self.assertIn("data-search-query-input", html)
        self.assertIn('name="type"', html)
        self.assertIn('name="sort"', html)
        self.assertIn("TMDb votes", html)
        self.assertIn("IMDb votes", html)
        self.assertIn('name="imdb_min"', html)
        self.assertIn('name="tmdb_min"', html)
        self.assertNotIn('name="trakt_min"', html)
        self.assertIn('class="result-card search-result-card"', html)
        self.assertIn('href="https://www.themoviedb.org/movie/603"', html)
        self.assertIn("The Matrix (1999)", html)
        self.assertIn("data-tmdb-card", html)
        self.assertIn("data-tmdb-watchlist-toggle", html)
        self.assertIn("search-watchlist-button", html)
        self.assertIn("static/tmdb.png", html)
        self.assertIn("<span>8.2 (28.0k)</span>", html)
        self.assertIn('aria-label="TMDb and IMDb title ratings"', html)
        self.assertIn("8.7 (2100000)", html)
        self.assertIn("imdb_icon.png", html)
        self.assertIn("static/tmdb_preview.js", html)
        self.assertNotIn("tmdb-preview-toolbar", html)
        self.assertNotIn("tmdb-preview-card", html)
        self.assertNotIn("TMDb preview search", html)

    def test_tmdb_mapped_show_keeps_rating_matrix_action_and_uses_tmdb_icon(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        mapped_show = TmdbCatalogItem(
            title_type="show",
            tmdb_id=82684,
            trakt_id=135985,
            title="Mapped TMDb Show",
            year=2026,
            tmdb_rating=8.2,
            tmdb_votes=28000,
            imdb_rating=8.7,
            imdb_votes=2_100_000,
            ratings_status="ready",
            released_at=datetime.now(tz=UTC) - timedelta(days=2),
            is_release_tracked=True,
        )
        self.app.state.services.tmdb_catalog.search_titles = (
            lambda query, title_type=None, *, page, limit: TmdbCatalogPage(
                items=[mapped_show],
                page=page,
                page_count=1,
            )
        )
        self.app.state.services.tmdb_catalog.local_release_items = lambda: [mapped_show]

        search_html = self.client.get("/search?q=Mapped+TMDb+Show&type=show").text
        release_html = self.client.get("/release-tracking").text

        for html in (search_html, release_html):
            card = html.split('data-tmdb-id="82684"', 1)[1].split("</article>", 1)[0]
            self.assertIn("title-matrix-trigger", card)
            self.assertIn("data-title-matrix-trigger", card)
            self.assertIn('data-title-matrix-tmdb-id="82684"', card)
            self.assertIn('/titles/tmdb/show/82684/episode-ratings-matrix', card)
            self.assertIn('href="https://www.themoviedb.org/tv/82684"', card)
            self.assertNotIn("trakt.tv", card)
            self.assertIn('static/tmdb.png', card)
            self.assertIn('poster-chip-icon poster-chip-icon-tmdb', card)
            self.assertNotIn(">TMDb 8.2", card)

        tmdb_icon = STATIC_DIR / "tmdb.png"
        self.assertTrue(tmdb_icon.is_file())
        icon_bytes = tmdb_icon.read_bytes()
        self.assertEqual(icon_bytes[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(
            (int.from_bytes(icon_bytes[16:20]), int.from_bytes(icon_bytes[20:24])),
            (64, 64),
        )
        styles = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        self.assertNotIn(
            ".poster-chip-icon-tmdb {\n    object-fit: cover;\n}",
            styles,
        )

    def test_tmdb_mapped_show_uses_only_imdb_season_layout(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"

        response = self.client.get("/titles/show/138748/episode-ratings-matrix")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("data-imdb-seasons-toggle", response.text)
        self.assertIn('data-matrix-layout-panel="imdb"', response.text)
        self.assertNotIn('data-matrix-layout-panel="trakt"', response.text)
        self.assertNotIn('data-title-matrix-provider="trakt"', response.text)
        self.assertIn('data-title-matrix-rating-mode="my"', response.text)
        self.assertNotIn("trakt", response.text.lower())

    def test_tmdb_mapped_show_matrix_uses_local_imdb_path_without_trakt_cache(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        mapped_show = TmdbCatalogItem(
            title_type="show",
            tmdb_id=82684,
            trakt_id=135985,
            imdb_id="tt13111078",
            title="Mapped TMDb Show",
            tmdb_rating=8.2,
            tmdb_votes=28_000,
            imdb_rating=8.7,
            imdb_votes=2_100_000,
            ratings_status="ready",
        )
        local_rating_calls: list[int] = []
        self.app.state.services.tmdb_catalog.get_item = lambda _title_type, _tmdb_id: mapped_show
        self.app.state.services.tmdb_catalog.local_show_episode_ratings = (
            lambda tmdb_id: local_rating_calls.append(tmdb_id) or {(1, 1): 9}
        )

        response = self.client.get("/titles/tmdb/show/82684/episode-ratings-matrix")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(local_rating_calls, [82684])
        self.assertEqual(
            self.matrix.calls[-1],
            {"imdb_id": "tt13111078", "title": "Mapped TMDb Show", "my_ratings": {(1, 1): 9}},
        )
        self.assertFalse(any(call.get("trakt_id") == 135985 for call in self.matrix.calls))
        self.assertNotIn('data-title-matrix-provider="trakt"', response.text)
        self.assertIn('data-title-matrix-rating-mode="my"', response.text)

    def test_tmdb_unmapped_show_keeps_rating_matrix_action_on_every_catalog_surface(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        unmapped_show = TmdbCatalogItem(
            title_type="show",
            tmdb_id=113962,
            imdb_id="tt13111078",
            title="Lioness",
            year=2023,
            tmdb_rating=8.0,
            tmdb_votes=1300,
            imdb_rating=7.8,
            imdb_votes=91000,
            ratings_status="ready",
            released_at=datetime.now(tz=UTC) - timedelta(days=2),
            is_release_tracked=True,
        )
        self.app.state.services.tmdb_catalog.search_titles = (
            lambda query, title_type=None, *, page, limit: TmdbCatalogPage(
                items=[unmapped_show],
                page=page,
                page_count=1,
            )
        )
        self.app.state.services.tmdb_catalog.explore_titles = (
            lambda title_type, feed, *, page, limit: TmdbCatalogPage(
                items=[unmapped_show],
                page=page,
                page_count=1,
            )
        )
        self.app.state.services.tmdb_catalog.local_release_items = lambda: [unmapped_show]
        self.app.state.services.tmdb_catalog.get_item = lambda title_type, tmdb_id: unmapped_show

        responses = (
            self.client.get("/search?q=Lioness&type=show"),
            self.client.get("/explore?type=show&feed=trending&page=1"),
            self.client.get("/release-tracking"),
        )

        for response in responses:
            self.assertEqual(response.status_code, 200)
            card = response.text.split('data-tmdb-id="113962"', 1)[1].split("</article>", 1)[0]
            self.assertIn('aria-label="Open episode IMDb ratings matrix for Lioness"', card)
            self.assertIn("title-matrix-trigger", card)
            self.assertIn("data-title-matrix-trigger", card)
            self.assertIn('/titles/tmdb/show/113962/episode-ratings-matrix', card)

        matrix_response = self.client.get("/titles/tmdb/show/113962/episode-ratings-matrix")
        self.assertEqual(matrix_response.status_code, 200)
        self.assertIn("S1", matrix_response.text)
        self.assertIn("E1", matrix_response.text)
        self.assertNotIn('data-title-matrix-provider="trakt"', matrix_response.text)
        self.assertIn('data-title-matrix-rating-mode="my"', matrix_response.text)

    def test_tmdb_preview_explore_reuses_existing_toolbar_and_card_layout(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"

        response = self.client.get("/explore?type=movie&feed=popular&page=1&imdb_min=7&tmdb_min=7")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("explore-toolbar-panel", html)
        self.assertIn("explore-media-tabs", html)
        self.assertIn("explore-feed-tabs", html)
        self.assertIn('name="imdb_min"', html)
        self.assertIn('name="tmdb_min"', html)
        self.assertNotIn('name="trakt_min"', html)
        self.assertIn('class="result-card search-result-card"', html)
        self.assertIn("TMDb Explore Title (2026)", html)
        self.assertIn("static/tmdb.png", html)
        self.assertIn("<span>7.8 (12.0k)</span>", html)
        self.assertIn("TMDb popularity 94.5", html)
        self.assertIn('aria-label="TMDb and IMDb title ratings"', html)
        self.assertIn("7.6 (14000)", html)
        self.assertIn("imdb_icon.png", html)
        self.assertIn("static/tmdb_preview.js", html)
        self.assertNotIn("tmdb-preview-toolbar", html)
        self.assertNotIn("tmdb-preview-card", html)

        trending = self.client.get("/explore?type=movie&feed=trending&page=1")
        self.assertIn("#1 weekly trend", trending.text)
        self.assertIn("TMDb popularity 94.5", trending.text)
        self.assertIn("weekly #1 · TMDb pop. 94.5", trending.text)

        anticipated = self.client.get("/explore?type=movie&feed=anticipated&page=1")
        self.assertIn("TMDb popularity 94.5", anticipated.text)

    def test_tmdb_preview_releases_reuses_existing_sections_and_cards(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"

        response = self.client.get("/release-tracking")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("release-tracking-section", html)
        self.assertIn("Released", html)
        self.assertIn("Awaiting release", html)
        self.assertIn('class="result-card search-result-card release-tracking-card', html)
        self.assertIn("TMDb Released Movie (2026)", html)
        self.assertIn("data-tmdb-card", html)
        self.assertIn("static/tmdb.png", html)
        self.assertIn("<span>7.5 (9.0k)</span>", html)
        self.assertIn("7.3 (11000)", html)
        self.assertIn("is-notification-sent", html)
        self.assertIn('data-notification-sent="true"', html)
        tmdb_script = (STATIC_DIR / "tmdb_preview.js").read_text(encoding="utf-8")
        self.assertIn('card.classList.toggle("is-unacknowledged", !acknowledged);', tmdb_script)
        self.assertIn('"is-notification-sent",', tmdb_script)
        self.assertIn('card.dataset.notificationSent === "true"', tmdb_script)
        self.assertNotIn("tmdb-preview-toolbar", html)
        self.assertNotIn("tmdb-preview-card", html)

    def test_combined_imdb_series_uses_safe_trakt_layout(self) -> None:
        self.matrix.imdb_layout_available = False
        self.search_watch.imdb_layout_available = False

        matrix = self.client.get("/titles/show/138748/episode-ratings-matrix")
        panel = self.client.get("/search/show/3/watch-panel")

        self.assertEqual(matrix.status_code, 200)
        self.assertIn('data-imdb-layout-available="0"', matrix.text)
        self.assertIn("IMDb seasons are unavailable", matrix.text)
        self.assertIn("data-imdb-seasons-toggle", matrix.text)
        self.assertIn('disabled data-layout-locked="1"', matrix.text)
        self.assertNotIn("is-imdb-seasons", matrix.text)
        self.assertNotIn('data-matrix-layout-panel="imdb"', matrix.text)
        self.assertIn('data-matrix-layout-panel="trakt"', matrix.text)

        self.assertEqual(panel.status_code, 200)
        self.assertIn('data-season-layout="trakt"', panel.text)
        self.assertIn('data-imdb-layout-available="0"', panel.text)
        self.assertIn("IMDb seasons are unavailable", panel.text)
        self.assertIn("data-search-watch-imdb-seasons-toggle", panel.text)
        self.assertIn("disabled", panel.text)
        self.assertNotIn("IMDb mapping is incomplete", panel.text)

    def test_imdb_seasons_preference_is_shared_and_strict(self) -> None:
        response = self.client.post("/ui/preferences/imdb-seasons", json={"enabled": False})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "enabled": False})
        self.assertEqual(self.saved_imdb_seasons, [False])
        self.assertFalse(self.app.state.services.auth.config.web_imdb_seasons_enabled)

        matrix = self.client.get("/titles/show/138748/episode-ratings-matrix")
        self.assertNotIn("data-imdb-seasons-toggle checked", matrix.text)
        self.assertIn('data-matrix-layout-panel="imdb" hidden', matrix.text)
        self.assertIn('data-matrix-layout-panel="trakt"', matrix.text)

        panel = self.client.get("/search/show/3/watch-panel")
        self.assertEqual(panel.status_code, 200)
        self.assertEqual(self.search_watch.load_calls[-1]["season_layout"], "trakt")
        self.assertIn('data-season-layout="trakt"', panel.text)

        invalid = self.client.post("/ui/preferences/imdb-seasons", json={"enabled": 1})
        extra = self.client.post("/ui/preferences/imdb-seasons", json={"enabled": True, "extra": False})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(extra.status_code, 400)
        self.assertFalse(self.app.state.services.auth.config.web_imdb_seasons_enabled)

    def test_imdb_seasons_preference_rolls_back_when_save_fails(self) -> None:
        self.app.state.runtime.config_store.save = lambda _config: (_ for _ in ()).throw(OSError("disk full"))

        response = self.client.post("/ui/preferences/imdb-seasons", json={"enabled": False})

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json()["enabled"])
        self.assertTrue(self.app.state.services.auth.config.web_imdb_seasons_enabled)

    def test_show_matrix_fragment_route_accepts_trakt_provider_refresh_missing(self) -> None:
        response = self.client.get("/titles/show/138748/episode-ratings-matrix?provider=trakt&refresh_missing=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.matrix.calls[-1],
            {
                "trakt_id": 138748,
                "force_refresh": False,
                "provider": "trakt",
                "refresh_missing": False,
                "allow_network_refresh": False,
            },
        )

    def test_release_page_opens_released_show_panel_and_renders_ratings_in_chip(self) -> None:
        response = self.client.get("/release-tracking")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertEqual(html.count("data-watch-panel-url="), 1)
        self.assertIn('data-watch-panel-url="/search/show/21/watch-panel"', html)
        self.assertIn('data-trakt-id="21"', html)
        self.assertNotIn('data-watch-panel-url="/search/show/22/watch-panel"', html)
        self.assertIn('href="https://trakt.tv/shows/upcoming-show"', html)
        self.assertIn("8.6 (860)", html)
        self.assertIn("8.2 (12400)", html)
        self.assertEqual(html.count("anticipated-rating-part"), 2)
        self.assertIn(
            "release-tracking-card is-unacknowledged is-notification-sent",
            html,
        )
        self.assertIn('data-notification-sent="true"', html)
        self.assertIn('id="release-watch-overlay"', html)
        self.assertIn("data-show-watch-play", html)
        release_script = (STATIC_DIR / "release_tracking_page.js").read_text(encoding="utf-8")
        self.assertIn("configurePlayAction(watchOverlay, trigger)", release_script)
        self.assertIn("show_watch_panel.js?v=", html)
        self.assertIn('data-title-matrix-url="/titles/show/21/episode-ratings-matrix"', html)
        self.assertIn('data-title-matrix-url="/titles/show/22/episode-ratings-matrix"', html)
        self.assertNotIn("scheduleWatchPanelRefreshIfPending", html)
        self.assertIn("refreshWatchPanel", release_script)
        self.assertIn('"is-notification-sent",', release_script)
        self.assertEqual(html.count('loading="lazy" decoding="async" fetchpriority="low"'), 1)
        styles = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        self.assertIn(".release-tracking-card.is-notification-sent .release-bell-icon", styles)
        notification_styles = styles[
            styles.index(".release-tracking-card.is-notification-sent .release-bell-icon") :
            styles.index(".anticipated-release-chip")
        ]
        self.assertIn("animation: release-notification-bell", notification_styles)
        self.assertIn("@keyframes release-notification-bell", notification_styles)
        self.assertNotIn("outline:", notification_styles)
        self.assertNotIn("release-notification-alert", notification_styles)

    def test_release_page_keeps_title_without_release_date_in_upcoming(self) -> None:
        self.release_items.append(
            TitleSummary(
                trakt_id=23,
                title_type="show",
                title="Unknown Release",
                slug="unknown-release",
                released_at=None,
            )
        )

        response = self.client.get("/release-tracking")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Unknown Release", response.text)
        self.assertIn("Release date unknown", response.text)
        self.assertNotIn("'NoneType' object has no attribute 'tzinfo'", response.text)

    def test_search_page_renders_without_waiting_for_metadata_enrichment(self) -> None:
        response = self.client.get("/search?q=test&type=all&sort=IMDb+votes")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('<select name="type" data-catalog-auto-submit>', html)
        self.assertIn('<select name="sort" data-catalog-auto-submit>', html)
        self.assertIn("Loading", html)
        self.assertIn("n/a", html)
        self.assertLess(html.index("Movie B"), html.index("Movie A"))
        self.assertIn('data-release-toggle data-title-type="movie" data-trakt-id="2"', html)
        self.assertIn('class="catalog-seen-overlay"', html)
        self.assertIn('static/seen.svg', html)
        self.assertIn('title="Remove from watched history"', html)
        self.assertIn('data-search-unwatch-action', html)
        self.assertIn('data-scope="title"', html)
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        seen_icon_rule = css.split(".catalog-seen-overlay img {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 60%", seen_icon_rule)
        self.assertIn("pointer-events: none", css.split(".catalog-seen-overlay {", 1)[1].split("}", 1)[0])
        self.assertIn("z-index: 2", css.split(".search-title-ratings-chip {", 1)[1].split("}", 1)[0])
        self.assertIn("z-index: 2", css.split(".search-result-poster > .history-rating-badge {", 1)[1].split("}", 1)[0])
        badge_rule = css.split(".user-rating-badge {", 1)[1].split("}", 1)[0]
        tail_rule = css.split(".user-rating-badge::after {", 1)[1].split("}", 1)[0]
        value_rule = css.split(".user-rating-value {", 1)[1].split("}", 1)[0]
        star_rule = css.split(".user-rating-star {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 0", badge_rule)
        self.assertIn("background: #fff", badge_rule)
        self.assertIn("padding: 4px 14px 5px 8px", badge_rule)
        self.assertIn("overflow: hidden", badge_rule)
        self.assertIn("width: 10px", tail_rule)
        self.assertIn("top: 2px", tail_rule)
        self.assertIn("right: 2px", tail_rule)
        self.assertIn("bottom: 2px", tail_rule)
        self.assertIn("border-radius: 0 10px 10px 0", tail_rule)
        self.assertIn("background: var(--user-rating-color", tail_rule)
        self.assertIn("color: #374151", value_rule)
        self.assertNotIn("font-weight", value_rule)
        self.assertIn("-webkit-text-stroke: 0", value_rule)
        self.assertNotIn("paint-order", value_rule)
        self.assertIn("text-shadow: none", value_rule)
        self.assertIn("color: #374151", star_rule)
        self.assertIn("-webkit-text-stroke: 0", star_rule)
        self.assertIn("text-shadow: none", star_rule)
        self.assertNotIn(".poster-average-rating-badge {", css)
        self.assertNotIn(".episode-user-rating-badge {", css)
        self.assertIn('--user-rating-color: rgb(24, 106, 59)', css)
        self.assertIn('--user-rating-color: rgb(40, 180, 99)', css)
        self.assertIn('--user-rating-color: rgb(244, 208, 63)', css)
        self.assertIn('--user-rating-color: rgb(243, 156, 18)', css)
        self.assertIn('--user-rating-color: rgb(231, 76, 60)', css)
        self.assertIn('--user-rating-color: rgb(99, 57, 116)', css)

    def test_search_rating_filters_apply_and_are_restored(self) -> None:
        response = self.client.get("/search?q=test&type=all&imdb_min=8&trakt_min=7")
        self.assertEqual(response.status_code, 200)
        self.assertIn("The Capture", response.text)
        self.assertNotIn("Movie A", response.text)
        self.assertIn('name="imdb_min" value="8"', response.text)
        self.assertIn('name="trakt_min" value="7"', response.text)
        self.assertIn('class="icon-button catalog-filter-reset"', response.text)
        self.assertIn('static/filter_reset.svg', response.text)
        self.assertNotIn("Clear filters", response.text)
        self.assertIn(":8:7", self.enrichment_calls[-1]["task_key"])
        self.assertFalse(self.enrichment_calls[-1]["save_search_state"])

        restored = self.client.get("/search?q=test&type=all")
        self.assertIn('name="imdb_min" value="8"', restored.text)
        self.assertIn('name="trakt_min" value="7"', restored.text)

    def test_catalog_filter_reset_links_explicitly_clear_saved_filters(self) -> None:
        search = self.client.get(
            "/search?q=test&type=all&imdb_min=8&trakt_min=7&hide_watchlisted=1&hide_history=1"
        )
        self.assertIn(
            "imdb_min=&trakt_min=&hide_watchlisted=0&hide_history=0",
            search.text,
        )

        cleared_search = self.client.get(
            "/search?q=test&type=all&imdb_min=&trakt_min=&hide_watchlisted=0&hide_history=0"
        )
        self.assertIn('name="imdb_min" value=""', cleared_search.text)
        self.assertIn('name="trakt_min" value=""', cleared_search.text)
        self.assertFalse(self.search_saved_filters["hide_watchlisted"])
        self.assertFalse(self.search_saved_filters["hide_history"])

        self.client.get(
            "/explore?type=show&feed=trending&imdb_min=8&trakt_min=7&hide_watchlisted=1&hide_history=1"
        )
        explore = self.client.get("/explore?type=show&feed=anticipated&hide_releases=1")
        self.assertIn(
            "feed=anticipated&page=1&hide_releases=0",
            explore.text,
        )

        self.client.get("/explore?type=show&feed=anticipated&hide_releases=0")
        self.assertEqual(self.explore_saved_filters["imdb_min"], "8")
        self.assertEqual(self.explore_saved_filters["trakt_min"], "7")
        self.assertTrue(self.explore_saved_filters["hide_watchlisted"])
        self.assertTrue(self.explore_saved_filters["hide_history"])
        self.assertFalse(self.explore_saved_filters["hide_releases"])

        cleared_explore = self.client.get(
            "/explore?type=show&feed=trending&imdb_min=&trakt_min=&hide_watchlisted=0&hide_history=0"
        )
        self.assertIn('name="imdb_min" value=""', cleared_explore.text)
        self.assertFalse(self.explore_saved_filters["hide_watchlisted"])
        self.assertFalse(self.explore_saved_filters["hide_history"])

    def test_search_page_renders_show_poster_trigger_and_movie_watch_button(self) -> None:
        response = self.client.get("/search?q=test&type=all&sort=IMDb+votes")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-search-show-watch-trigger', html)
        self.assertIn('data-watch-panel-url="/search/show/3/watch-panel"', html)
        self.assertIn('data-title-type="movie"', html)
        self.assertIn('data-title-type="show"', html)
        self.assertIn('data-search-watch-action', html)
        self.assertIn('data-search-watch-date-mode="now"', html)
        self.assertIn('data-search-watch-date-mode="custom"', html)
        self.assertNotIn('data-search-watch-date-mode="none"', html)
        self.assertIn("data-search-watch-header-mark", html)
        self.assertIn("data-search-watch-header-unwatch", html)
        season_mark_index = html.index("data-search-watch-header-season-mark")
        mark_all_index = html.index("data-search-watch-header-mark")
        season_unwatch_index = html.index("data-search-watch-header-season-unwatch")
        unwatch_all_index = html.index("data-search-watch-header-unwatch")
        play_index = html.index("data-show-watch-play")
        self.assertLess(season_mark_index, mark_all_index)
        self.assertLess(mark_all_index, season_unwatch_index)
        self.assertLess(season_unwatch_index, unwatch_all_index)
        self.assertLess(unwatch_all_index, play_index)
        self.assertIn('/search/movie/1/play?title=Movie%20A', html)
        self.assertIn('/search/show/3/play?title=The%20Capture', html)
        self.assertIn('class="history-rating-badge user-rating-badge poster-average-rating-badge"', html)
        self.assertIn('style="--user-rating-color: rgb(40, 180, 99);"', html)
        self.assertIn('<span class="user-rating-value">8.5</span>', html)
        self.assertIn('<span class="user-rating-star">&#9733;</span>', html)
        self.assertIn('class="history-rating-badge user-rating-badge"', html)
        self.assertIn('style="--user-rating-color: rgb(24, 106, 59);"', html)
        self.assertIn('<span class="user-rating-value">9</span>', html)
        self.assertIn('/cached-image?url=https%3A//poster.example/capture.jpg&amp;v=3', html)
        self.assertIn('/cached-image?url=https%3A//poster.example/movie-a.jpg&amp;v=3', html)
        self.assertEqual(html.count('decoding="async"'), 2)
        self.assertEqual(html.count('fetchpriority="low"'), 2)
        ui_script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")
        self.assertIn('current.pathname !== "/cached-image"', ui_script)
        self.assertNotIn("data-direct-src", html)
        self.assertNotIn("dataset.directSrc", html)
        self.assertIn('data-watchlisted="true"', html)
        self.assertIn('data-watchlisted="false"', html)
        self.assertIn("bookmark.svg", html)
        self.assertIn("bookmark_unfill.svg", html)
        self.assertIn('src="http://testserver/static/bookmark_unfill.svg"', html)

    def test_unwatched_show_mark_button_opens_episode_panel(self) -> None:
        response = self.client.get("/search?q=test&type=all&sort=IMDb+votes")

        self.assertEqual(response.status_code, 200)
        button = response.text.split('aria-label="Mark The Capture watched"', 1)[0].rsplit("<button", 1)[1]
        self.assertIn("data-search-show-watch-trigger", button)
        self.assertIn('data-watch-panel-url="/search/show/3/watch-panel"', button)
        self.assertNotIn("data-search-watch-action", button)

    def test_search_shell_restores_saved_query_and_type_in_toolbar(self) -> None:
        self.app.state.services.catalog.load_last_search_state = lambda: {
            "query": "The Capture",
            "title_type": "show",
            "sort_mode": "IMDb votes",
            "imdb_min": "",
            "trakt_min": "",
            "results": [],
        }

        response = self.client.get("/search?catalog_shell=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="The Capture"', response.text)
        self.assertIn('<option value="show" selected>Shows</option>', response.text)
        self.assertIn('data-catalog-loading="1"', response.text)

    def test_restored_search_enrichment_cannot_overwrite_newer_last_search(self) -> None:
        self.app.state.services.catalog.load_last_search_state = lambda: {
            "query": "Old query",
            "title_type": "show",
            "sort_mode": "IMDb votes",
            "imdb_min": "",
            "trakt_min": "",
            "results": [TitleSummary(trakt_id=3, title_type="show", title="The Capture")],
        }

        response = self.client.get("/search")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.enrichment_calls[-1]["save_search_state"])

    def test_search_recent_queries_render_below_input_with_filters_visible(self) -> None:
        self.app.state.services.catalog.search_history = lambda: ["Dune", "Severance"]

        response = self.client.get("/search?q=test&type=all")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("data-search-query-input", html)
        self.assertIn('id="search-recent-queries"', html)
        self.assertIn('data-search-recent-query="Dune"', html)
        self.assertIn('data-search-recent-query="Severance"', html)
        self.assertIn('class="catalog-filter-controls"', html)
        self.assertNotIn('class="catalog-filter-details"', html)
        self.assertNotIn("Recent queries</h3>", html)
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        toolbar_rule = css.split(".search-toolbar-panel {", 1)[1].split("}", 1)[0]
        self.assertIn("position: relative", toolbar_rule)
        self.assertIn("z-index: 30", toolbar_rule)

    def test_legacy_title_details_route_redirects_to_trakt(self) -> None:
        response = self.client.get("/titles/movie/935748", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://trakt.tv/movies/935748")

    def test_large_templates_load_static_modules_without_inline_script_blocks(self) -> None:
        templates = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        for name in (
            "base.html",
            "search_v2.html",
            "history.html",
            "progress.html",
            "release_tracking.html",
            "settings.html",
            "setup.html",
            "reauthorization_required.html",
            "history_show_watch_overlay.html",
        ):
            self.assertNotIn("<script>", (templates / name).read_text(encoding="utf-8"), name)
        self.assertTrue((STATIC_DIR / "ui_core.js").is_file())
        self.assertTrue((STATIC_DIR / "catalog_page.js").is_file())

    def test_tmdb_watch_panel_templates_load_shared_handler(self) -> None:
        templates = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        trigger_templates = []
        for template_path in templates.glob("*.html"):
            template = template_path.read_text(encoding="utf-8")
            if "data-tmdb-watch-panel" not in template:
                continue
            trigger_templates.append(template_path.name)
            self.assertIn("tmdb_preview.js", template, template_path.name)

        self.assertTrue(trigger_templates)

    def test_tmdb_watch_panel_client_resets_and_configures_every_header_action(self) -> None:
        script = (STATIC_DIR / "tmdb_preview.js").read_text(encoding="utf-8")

        self.assertIn("function resetPanelHeader()", script)
        self.assertIn("let panelRequestToken = 0;", script)
        self.assertIn('overlay.dataset.watchPanelOwner = "tmdb"', script)
        self.assertIn('overlay?.dataset.watchPanelOwner !== "tmdb"', script)
        self.assertIn("{onEscape: closeTmdbPanel}", script)
        self.assertIn("resetPanelHeader();", script)
        self.assertIn("configurePanelHeader(data, card)", script)
        self.assertIn("/titles/tmdb/show/${tmdbId}/episode-ratings-matrix", script)
        self.assertIn("/search/show/${tmdbId}/play?title=", script)
        self.assertIn("data-tmdb-scope-action", script)
        self.assertIn("data-tmdb-scope-unwatch", script)
        for adapter_name in ("catalog_page.js", "release_tracking_page.js"):
            adapter = (STATIC_DIR / adapter_name).read_text(encoding="utf-8")
            self.assertIn('watchOverlay.dataset.watchPanelOwner = "trakt"', adapter)
            self.assertIn('watchOverlay?.dataset.watchPanelOwner !== "trakt"', adapter)

    def test_tmdb_watch_panel_route_exposes_standard_header_contract(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        self.app.state.services.tmdb_catalog.load_watch_panel = lambda tmdb_id, *, season=None: {
            "tmdb_id": tmdb_id,
            "trakt_id": 8675309,
            "title": "Guilty Crown",
            "selected_season": season or 1,
            "seasons": [{"season_number": 1, "episode_count": 22}],
            "episodes": [],
            "tmdb_rating": 7.4,
            "tmdb_votes": 2300,
            "imdb_rating": 7.0,
            "imdb_votes": 18100,
            "ratings_status": "ready",
            "watched_count": 0,
            "released_count": 22,
            "released_watched_count": 0,
            "can_mark_title": True,
            "can_unwatch_title": False,
            "can_mark_season": True,
            "can_unwatch_season": False,
        }

        response = self.client.get("/tmdb-preview/show/43125/watch-panel")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["tmdb_id"], 43125)
        self.assertEqual(payload["tmdb_rating"], 7.4)
        self.assertEqual(payload["imdb_rating"], 7.0)
        self.assertEqual(payload["released_count"], 22)
        self.assertTrue(payload["can_mark_title"])
        self.assertTrue(payload["can_mark_season"])
        self.assertNotIn("trakt_id", payload)

    def test_tmdb_episode_watch_returns_rating_context_and_release_cleanup(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        item = TmdbCatalogItem(
            title_type="show",
            tmdb_id=43125,
            title="Guilty Crown",
            is_release_tracked=True,
        )
        watch_calls: list[dict] = []
        self.app.state.services.tmdb_catalog.get_item = lambda _title_type, _tmdb_id: item
        self.app.state.services.tmdb_catalog.mark_watched = lambda _item, **kwargs: (
            watch_calls.append(kwargs)
            or {
                "local_only": True,
                "trakt_id": None,
                "mapped": False,
                "removed_from_release_tracking": True,
            }
        )

        response = self.client.post(
            "/tmdb-preview/watch",
            json={
                "title_type": "show",
                "tmdb_id": 43125,
                "season": 1,
                "episode": 2,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["removed_from_release_tracking"])
        self.assertEqual(
            payload["rating_context"],
            {
                "provider": "tmdb",
                "title_type": "show",
                "tmdb_id": 43125,
                "title": "Guilty Crown",
                "season": 1,
                "episode": 2,
            },
        )
        self.assertEqual(watch_calls[0]["season"], 1)
        self.assertEqual(watch_calls[0]["episode"], 2)
        script = (STATIC_DIR / "tmdb_preview.js").read_text(encoding="utf-8")
        self.assertIn("window.traktOpenRatingModal", script)
        self.assertIn("removed_from_release_tracking", script)

    def test_partial_catalog_navigation_replaces_active_navigation(self) -> None:
        script = (STATIC_DIR / "catalog_page.js").read_text(encoding="utf-8")
        ui_script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")

        self.assertIn("setActiveNavigation(target.pathname)", script)
        self.assertIn('"X-Trakt-Partial": "catalog"', script)
        self.assertIn("window.showTraktReconnectPrompt?.(payload)", script)
        self.assertIn('const incomingNav = parsed.querySelector(".nav")', script)
        self.assertIn('const currentNav = document.querySelector(".nav")', script)
        self.assertIn("currentNav.replaceWith(incomingNav)", script)
        self.assertIn('headers.set("X-Trakt-Fetch", "1")', ui_script)
        self.assertIn("showTraktReconnectPrompt(payload)", ui_script)
        self.assertIn('form.action = "/settings/trakt-authorize"', ui_script)

    def test_catalog_pager_scrolls_new_results_to_start(self) -> None:
        script = (STATIC_DIR / "catalog_page.js").read_text(encoding="utf-8")

        self.assertIn("scrollToPageStart = false", script)
        self.assertIn(
            'const scrollToPageStart = Boolean(link.closest(".pager"));',
            script,
        )
        self.assertIn(
            "navigateCatalog(target, {scrollToPageStart});",
            script,
        )
        self.assertIn(
            'resultsRegion?.scrollIntoView({block: "start", behavior: "auto"});',
            script,
        )

    def test_watchlist_page_renders_saved_titles_and_active_navigation(self) -> None:
        response = self.client.get("/watchlist")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("The Capture", html)
        self.assertIn('href="/watchlist?catalog_shell=1" class="active" aria-current="page"', html)
        self.assertIn('data-watchlisted="true"', html)
        self.assertIn('src="http://testserver/static/bookmark.svg"', html)
        self.assertNotIn('class="catalog-seen-overlay"', html)
        self.assertNotIn("Movies and shows saved for later", html)
        self.assertNotIn("<h2>Watchlist</h2>", html)
        self.assertIn('<select name="type" data-catalog-auto-submit>', html)
        self.assertIn('<select name="release" data-catalog-auto-submit>', html)
        self.assertIn('data-watchlist-direction-toggle', html)
        self.assertIn('data-watchlist-direction-input', html)
        self.assertIn("Recently added", html)
        self.assertIn("Release date", html)
        self.assertIn("IMDb rating", html)
        self.assertIn("Alphabetical", html)
        self.assertIn("Released", html)
        self.assertIn("Upcoming", html)
        self.assertIn(">↓</button>", html)
        self.assertNotIn(">Apply<", html)

    def test_tmdb_mode_watchlist_includes_local_tmdb_titles(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        self.app.state.services.tmdb_catalog.local_watchlist_items = lambda: [
            TmdbCatalogItem(
                title_type="show",
                tmdb_id=43125,
                title="Guilty Crown",
                year=2011,
                is_watchlisted=True,
                poster_url="https://poster.example/guilty-crown.jpg",
                released_at=datetime(2011, 10, 14, tzinfo=UTC),
            )
        ]

        response = self.client.get("/watchlist")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Guilty Crown (2011)", response.text)
        self.assertIn('data-tmdb-id="43125"', response.text)
        self.assertIn('data-tmdb-watchlisted="true"', response.text)

    def test_explore_defaults_to_anticipated_shows_and_hides_future_actions(self) -> None:
        response = self.client.get("/explore")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('href="/explore?catalog_shell=1" class="active" aria-current="page"', html)
        self.assertIn('class="chip active" href="/explore?type=show&feed=anticipated&page=1&hide_releases=0"', html)
        self.assertIn("Future Show", html)
        self.assertIn("> 1.2k</span>", html)
        self.assertNotIn("1.2k lists", html)
        self.assertIn("anticipated-release-chip", html)
        self.assertNotIn("Play Future Show", html)
        self.assertNotIn("Mark Future Show watched", html)
        self.assertIn('href="https://trakt.tv/shows/future-show"', html)
        self.assertNotIn('name="imdb_min"', html)
        self.assertNotIn('name="trakt_min"', html)
        self.assertIn("Not in Releases", html)
        self.assertNotIn("Not in History", html)
        self.assertNotIn(">Apply<", html)
        self.assertIn('class="explore-tabs explore-media-tabs"', html)
        self.assertIn('class="explore-tabs explore-feed-tabs"', html)

    def test_explore_trending_movies_renders_metrics_actions_and_pager(self) -> None:
        response = self.client.get("/explore?type=movie&feed=trending&page=2")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Current Movie", html)
        self.assertIn("1.2k watching", html)
        self.assertIn("Play Current Movie", html)
        self.assertIn("Mark Current Movie watched", html)
        self.assertIn('/explore?type=movie&feed=trending&page=1', html)
        self.assertIn('/explore?type=movie&feed=trending&page=3', html)

    def test_explore_trending_filters_by_minimum_ratings_and_preserves_values(self) -> None:
        response = self.client.get("/explore?type=movie&feed=trending&imdb_min=8&trakt_min=7")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Current Movie", html)
        self.assertIn('name="imdb_min" value="8"', html)
        self.assertIn('name="trakt_min" value="7"', html)
        self.assertIn('aria-label="Reset Explore filters"', html)
        self.assertIn('static/filter_reset.svg', html)
        self.assertIn('feed=popular&page=1&hide_watchlisted=0&hide_history=0&imdb_min=8&trakt_min=7', html)
        self.assertIn('page=2&hide_watchlisted=0&hide_history=0&imdb_min=8&trakt_min=7', html)
        self.assertNotIn(">Apply<", html)

        below_imdb = self.client.get("/explore?type=movie&feed=trending&imdb_min=8.3")
        self.assertEqual(below_imdb.status_code, 200)
        self.assertNotIn("Current Movie", below_imdb.text)
        self.assertIn("No titles on this page match the selected rating filters.", below_imdb.text)
        self.assertIn('page=2&imdb_min=8.3&trakt_min=7&hide_watchlisted=0&hide_history=0', below_imdb.text)

        below_trakt = self.client.get("/explore?type=movie&feed=trending&trakt_min=7.5")
        self.assertEqual(below_trakt.status_code, 200)
        self.assertNotIn("Current Movie", below_trakt.text)

    def test_explore_rating_filters_ignore_empty_and_invalid_thresholds(self) -> None:
        response = self.client.get("/explore?type=show&feed=popular&imdb_min=&trakt_min=20")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Future Show", response.text)
        self.assertIn('name="imdb_min" value=""', response.text)
        self.assertIn('name="trakt_min" value=""', response.text)

    def test_explore_rating_filters_are_restored_after_returning_to_explore(self) -> None:
        self.client.get("/explore?type=show&feed=trending&imdb_min=8.2&trakt_min=7.5")
        anticipated = self.client.get("/explore")
        self.assertIn('feed=trending&page=1&hide_watchlisted=0&hide_history=0&imdb_min=8.2&trakt_min=7.5', anticipated.text)

        trending = self.client.get("/explore?type=show&feed=trending")
        self.assertIn('name="imdb_min" value="8.2"', trending.text)
        self.assertIn('name="trakt_min" value="7.5"', trending.text)

    def test_explore_invalid_parameters_use_default_feed_and_type(self) -> None:
        response = self.client.get("/explore?type=bad&feed=bad&page=invalid")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Future Show", html)
        self.assertIn('href="/explore?type=show&feed=anticipated&page=1&hide_releases=0"', html)

    def test_explore_exclusion_filters_hide_watchlist_and_history_titles(self) -> None:
        self.release_tracking_keys.add(("show", 5))
        anticipated = self.client.get("/explore?type=show&feed=anticipated&hide_releases=1")
        self.assertNotIn("Future Show", anticipated.text)
        self.assertIn('name="hide_releases" value="1" checked', anticipated.text)

        self.release_tracking_keys.discard(("show", 5))
        self.catalog_history_keys.add(("show", 5))
        trending = self.client.get("/explore?type=show&feed=trending&hide_watchlisted=0&hide_history=1")
        self.assertNotIn("Future Show", trending.text)
        self.assertIn('name="hide_history" value="1" checked', trending.text)

    def test_explore_anticipated_release_exclusion_persists_with_checkbox_values(self) -> None:
        self.release_tracking_keys.add(("show", 5))

        selected = self.client.get("/explore?type=show&feed=anticipated&hide_releases=1&hide_releases=0")

        self.assertEqual(selected.status_code, 200)
        self.assertNotIn("Future Show", selected.text)
        self.assertIn('name="hide_releases" value="1" checked', selected.text)

        restored = self.client.get("/explore?type=show&feed=anticipated")

        self.assertEqual(restored.status_code, 200)
        self.assertNotIn("Future Show", restored.text)
        self.assertIn('name="hide_releases" value="1" checked', restored.text)

        trending = self.client.get("/explore?type=show&feed=trending")

        self.assertEqual(trending.status_code, 200)
        self.assertIn('feed=anticipated&page=1&hide_releases=1', trending.text)

        returned = self.client.get("/explore?type=show&feed=anticipated&hide_releases=1")

        self.assertEqual(returned.status_code, 200)
        self.assertNotIn("Future Show", returned.text)
        self.assertIn('name="hide_releases" value="1" checked', returned.text)

    def test_search_exclusion_filters_hide_watchlist_and_history_titles(self) -> None:
        response = self.client.get("/search?q=test&type=all&hide_watchlisted=1&hide_history=1")
        self.assertEqual(response.status_code, 200)
        self.assertLess(response.text.index("Minimum Trakt rating"), response.text.index("Not in Watchlist"))
        self.assertLess(response.text.index("Not in History"), response.text.index("search-submit-button"))
        self.assertIn("Movie B", response.text)
        self.assertNotIn("Movie A", response.text)
        self.assertNotIn("The Capture", response.text)

    def test_explore_popular_does_not_render_a_fake_metric(self) -> None:
        response = self.client.get("/explore?type=show&feed=popular")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="explore-rank-metric"', response.text)

    def test_watchlist_direction_button_reflects_ascending_query(self) -> None:
        response = self.client.get("/watchlist?direction=asc")
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="asc" data-watchlist-direction-input', response.text)
        self.assertIn(">↑</button>", response.text)
        self.assertIn('title="Ascending"', response.text)

    def test_watchlist_page_filters_type(self) -> None:
        response = self.client.get("/watchlist?type=movie&sort=Alphabetical")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Arrival", response.text)
        self.assertNotIn("The Capture", response.text)
        self.assertIn('<option value="movie" selected>', response.text)
        self.assertIn('aria-label="Reset Watchlist filters"', response.text)

    def test_watchlist_page_filters_release_state(self) -> None:
        upcoming = self.client.get("/watchlist?release=upcoming")
        self.assertEqual(upcoming.status_code, 200)
        self.assertIn("The Capture", upcoming.text)
        self.assertNotIn("Arrival", upcoming.text)

        released = self.client.get("/watchlist?release=released")
        self.assertEqual(released.status_code, 200)
        self.assertIn("Arrival", released.text)
        self.assertNotIn("The Capture", released.text)

    def test_filtered_empty_watchlist_does_not_claim_the_whole_watchlist_is_empty(self) -> None:
        response = self.client.get("/watchlist?type=movie&release=upcoming")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No titles match the selected filters.", response.text)
        self.assertNotIn("Your watchlist is empty", response.text)

    def test_watchlist_toggle_removes_title(self) -> None:
        response = self.client.post(
            "/watchlist/toggle",
            json={"title_type": "show", "trakt_id": 3, "watchlisted": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.watchlist_calls[-1],
            {"title_type": "show", "trakt_id": 3, "watchlisted": False},
        )
        self.assertFalse(response.json()["watchlisted"])

    def test_bookmark_icon_has_bounded_dimensions(self) -> None:
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        button_rule = css.split(".search-watchlist-button {", 1)[1].split("}", 1)[0]
        unfilled_rule = css.split(".search-watchlist-button .icon-glyph-bookmark.is-unfilled {", 1)[1].split("}", 1)[0]
        filled_rule = css.split(".search-watchlist-button .icon-glyph-bookmark.is-filled {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 28px", button_rule)
        self.assertIn("height: 24px", button_rule)
        self.assertIn("right: auto", button_rule)
        self.assertIn("left: 8px", button_rule)
        self.assertIn("width: 11px", unfilled_rule)
        self.assertIn("height: 12px", unfilled_rule)
        self.assertIn("width: 13px", filled_rule)
        self.assertIn("height: 13px", filled_rule)

    def test_repeated_catalog_cards_do_not_use_backdrop_blur(self) -> None:
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

        self.assertIn(".panel,\n.banner {\n    backdrop-filter: blur(14px);", css)
        self.assertNotIn(".panel,\n.banner,\n.result-card {\n    backdrop-filter", css)

    def test_pending_cached_images_retry_frequently_without_shortening_the_window(self) -> None:
        script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")

        self.assertIn("const CACHED_IMAGE_RETRY_LIMIT = 30;", script)
        self.assertIn("const CACHED_IMAGE_RETRY_DELAY_MS = 750;", script)
        self.assertEqual(script.count("attempts >= CACHED_IMAGE_RETRY_LIMIT"), 2)
        self.assertEqual(script.count("}, CACHED_IMAGE_RETRY_DELAY_MS);"), 2)
        self.assertNotIn("Math.min(5000, 600 * (attempts + 1))", script)

    def test_search_show_watch_panel_fragment_renders_default_season_cards(self) -> None:
        response = self.client.get("/search/show/3/watch-panel")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-search-watch-season-tab="1"', html)
        self.assertIn('data-search-watch-trakt-rating="8.2 (1200)"', html)
        self.assertIn('data-search-watch-imdb-rating="8.4 (3400)"', html)
        self.assertIn('data-search-watch-season-tab="0"', html)
        self.assertIn('data-search-watch-season-panel="1"', html)
        self.assertIn("Pilot", html)
        self.assertIn('data-season-layout="imdb"', html)
        self.assertIn("data-search-watch-imdb-seasons-toggle", html)
        episode_labels = [
            " ".join(part.split("</span>", 1)[0].split())
            for part in html.split('<span class="search-watch-episode-label">')[1:]
        ]
        self.assertIn("S02E01", episode_labels)
        self.assertIn("S01E02", episode_labels)
        self.assertTrue(all("IMDb" not in label and "Trakt" not in label for label in episode_labels))
        self.assertTrue(all("Unmapped" not in label for label in episode_labels))
        self.assertIn("No preview", html)
        self.assertIn('data-still-pending="1"', html)
        season_one_tab = html.split('data-search-watch-season-tab="1"', 1)[1].split("</button>", 1)[0]
        self.assertIn('data-search-watch-season-label="S1"', season_one_tab)
        self.assertIn('data-search-watch-season-can-mark="0"', season_one_tab)
        self.assertIn('data-search-watch-season-can-unwatch="1"', season_one_tab)
        self.assertNotIn("search-watch-bulk-actions", html)
        self.assertIn('data-season-layout="imdb"', html)
        self.assertIn('class="search-watch-episode-card is-watched"', html)
        self.assertIn('class="search-watch-seen-overlay"', html)
        self.assertIn(
            'href="https://trakt.tv/shows/the-capture/seasons/1/episodes/1"',
            html,
        )
        self.assertIn('aria-label="Open The Capture S01E01 on Trakt"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('viewBox="0 6 32 20" preserveAspectRatio="xMidYMid meet"', html)
        self.assertIn('width="60%" height="60%"', html)
        self.assertIn('/cached-image?url=https%3A//still.example/pilot.jpg&amp;v=3', html)
        ui_script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")
        self.assertIn('current.pathname !== "/cached-image"', ui_script)
        self.assertNotIn("data-direct-src", html)
        self.assertNotIn("dataset.directSrc", html)
        self.assertEqual(self.search_watch.enrich_still_calls, [])
        self.assertIn("data-search-unwatch-action", html)
        self.assertNotIn('data-scope="season"', html)
        self.assertNotIn("Mark season", html)
        self.assertNotIn("Undo season", html)
        self.assertNotIn("Undo entire series", html)
        self.assertIn("seen.svg", html)
        self.assertIn("cancel.svg", html)
        self.assertIn(
            'class="history-rating-badge user-rating-badge episode-user-rating-badge search-watch-user-rating"',
            html,
        )
        self.assertIn('data-rating-season="1"', html)
        self.assertIn('data-rating-episode="1"', html)
        self.assertIn('data-user-rating="8"', html)
        self.assertIn('<span class="user-rating-value">8</span>', html)
        self.assertIn('<span class="user-rating-star">&#9733;</span>', html)
        self.assertIn('class="history-rate-chip search-watch-user-rating"', html)
        self.assertIn(">Rate</button>", html)
        page = self.client.get("/search?q=test&type=all")
        self.assertIn("data-search-watch-title-ratings", page.text)
        self.assertIn("data-title-matrix-title-ratings", page.text)
        panel_script = (STATIC_DIR / "show_watch_panel.js").read_text(encoding="utf-8")
        self.assertIn("configureTitleRatings(overlay, body)", panel_script)
        self.assertIn("episode-ratings-matrix", panel_script)
        self.assertIn("syncTitleMatrixTitleRatings", ui_script)
        self.assertIn("function renderSavedRating(trigger, rating)", ui_script)
        self.assertIn('trigger.classList.add("user-rating-badge")', ui_script)
        self.assertIn('trigger.dataset.userRating = String(rating)', ui_script)
        self.assertIn('value.className = "user-rating-value"', ui_script)
        self.assertIn('star.className = "user-rating-star"', ui_script)

    def test_watch_panel_spoilers_require_history_and_only_blur_after_frontier(self) -> None:
        self.app.state.services.auth.config.web_hide_spoilers = True
        no_frontier = self.client.get("/search/show/3/watch-panel")
        self.assertNotIn("data-spoiler-key", no_frontier.text)

        original_load = self.search_watch.load_show_panel

        def load_with_frontier(*args, **kwargs):
            panel = original_load(*args, **kwargs)
            panel.watched_frontier_key = (1, 2)
            special = next(season for season in panel.seasons if season.season == 0).episodes[0]
            special.still_url = "https://still.example/special.jpg"
            season_one = next(season for season in panel.seasons if season.season == 1)
            season_one.episodes[0].is_watched = False
            season_one.episodes.append(
                SearchWatchEpisode(
                    season=1,
                    number=3,
                    title="After Frontier",
                    still_url="https://still.example/after.jpg",
                )
            )
            panel.seasons.append(
                SearchWatchSeason(
                    season=2,
                    label="S2",
                    episodes=[
                        SearchWatchEpisode(
                            season=2,
                            number=1,
                            title="Later Season",
                            still_url="https://still.example/later.jpg",
                        )
                    ],
                )
            )
            return panel

        self.search_watch.load_show_panel = load_with_frontier
        try:
            protected = self.client.get("/search/show/3/watch-panel").text
            self.assertIn('data-spoiler-key="test-user:3:1:3"', protected)
            self.assertIn('data-spoiler-key="test-user:3:2:1"', protected)
            self.assertNotIn('data-spoiler-key="test-user:3:1:1"', protected)
            self.assertNotIn('data-spoiler-key="test-user:3:0:1"', protected)
            self.assertEqual(protected.count("Click to unblur"), 2)

            self.app.state.services.auth.config.web_hide_spoilers = False
            unprotected = self.client.get("/search/show/3/watch-panel").text
            self.assertNotIn("data-spoiler-key", unprotected)
        finally:
            self.search_watch.load_show_panel = original_load

        ui_script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")
        self.assertIn("window.sessionStorage", ui_script)
        self.assertIn('new MutationObserver((records) => {', ui_script)
        self.assertIn('event.target.closest("[data-spoiler-reveal]")', ui_script)

    def test_watch_panel_title_ratings_precede_header_actions_and_matrix_stacks_above_panel(self) -> None:
        template_paths = (
            PROJECT_ROOT / "trakt_tracker" / "web" / "templates" / "search_v2.html",
            PROJECT_ROOT / "trakt_tracker" / "web" / "templates" / "history_show_watch_overlay.html",
            PROJECT_ROOT / "trakt_tracker" / "web" / "templates" / "release_tracking.html",
        )
        for template_path in template_paths:
            template = template_path.read_text(encoding="utf-8")
            actions_index = template.index('class="search-watch-header-actions"')
            ratings_index = template.index("data-search-watch-title-ratings")
            season_mark_index = template.index("data-search-watch-header-season-mark")
            mark_all_index = template.index("data-search-watch-header-mark")
            season_unwatch_index = template.index("data-search-watch-header-season-unwatch")
            unwatch_all_index = template.index("data-search-watch-header-unwatch")
            play_index = template.index("data-show-watch-play")
            self.assertLess(actions_index, ratings_index, template_path.name)
            self.assertLess(ratings_index, season_mark_index, template_path.name)
            self.assertLess(season_mark_index, mark_all_index, template_path.name)
            self.assertLess(mark_all_index, season_unwatch_index, template_path.name)
            self.assertLess(season_unwatch_index, unwatch_all_index, template_path.name)
            self.assertLess(unwatch_all_index, play_index, template_path.name)

        adapter_paths = (
            STATIC_DIR / "catalog_page.js",
            STATIC_DIR / "history_watch_panel.js",
            STATIC_DIR / "release_tracking_page.js",
        )
        for adapter_path in adapter_paths:
            adapter = adapter_path.read_text(encoding="utf-8")
            self.assertIn(
                "configureScopeActions(watchOverlay, trigger, null)",
                adapter,
                adapter_path.name,
            )

        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        matrix_rule = css.split(".title-matrix-overlay {", 1)[1].split("}", 1)[0]
        watch_rule = css.split(".search-watch-overlay,", 1)[1].split("}", 1)[0]
        matrix_z_index = int(matrix_rule.split("z-index:", 1)[1].split(";", 1)[0].strip())
        watch_z_index = int(watch_rule.split("z-index:", 1)[1].split(";", 1)[0].strip())
        self.assertGreater(matrix_z_index, watch_z_index)
        tooltip_rule = css.split(".title-matrix-tooltip {", 1)[1].split("}", 1)[0]
        tooltip_z_index = int(tooltip_rule.split("z-index:", 1)[1].split(";", 1)[0].strip())
        self.assertGreater(tooltip_z_index, matrix_z_index)

    def test_search_watch_waits_for_panel_rate_trigger_before_auto_rating(self) -> None:
        template = (STATIC_DIR / "catalog_page.js").read_text(encoding="utf-8")

        self.assertIn('await loadWatchPanel("", {preserve: true});', template)

    def test_episode_ratings_matrix_dialog_keeps_the_grid_body_visible(self) -> None:
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        dialog_rule = css.split(".title-matrix-dialog {", 1)[1].split("}", 1)[0]

        self.assertIn("grid-template-rows: auto auto;", dialog_rule)
        self.assertIn("max-height: min(620px, calc(100vh - 200px));", css)

    def test_catalog_watchlist_removal_resolves_show_card_from_panel_action(self) -> None:
        script = (STATIC_DIR / "catalog_page.js").read_text(encoding="utf-8")

        self.assertIn("action.dataset?.traktId || action.trakt_id", script)
        self.assertIn("const card = catalogCardForAction(completedAction);", script)
        self.assertIn("setWatchlistButtonState(watchlistButton, false);", script)

    def test_search_unwatch_returns_restore_payload(self) -> None:
        response = self.client.post(
            "/search/unwatch",
            json={"trakt_id": 3, "season": 1, "episode": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.unmark_calls, [{"trakt_id": 3, "season": 1, "episode": 1}])
        self.assertEqual(response.json()["restore"]["watched_at"], "2026-07-01T12:00:00+00:00")

    def test_search_unwatch_movie_uses_title_scope(self) -> None:
        response = self.client.post(
            "/search/unwatch",
            json={"title_type": "movie", "trakt_id": 1, "scope": "title"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.search_watch.unmark_scope_calls,
            [
                {
                    "title_type": "movie",
                    "trakt_id": 1,
                    "scope": "title",
                    "season": None,
                    "season_layout": "trakt",
                }
            ],
        )
        self.assertFalse(response.json()["still_watched"])

    def test_search_season_actions_forward_imdb_layout(self) -> None:
        watched = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "title": "The Capture",
                "scope": "season",
                "season": 2,
                "season_layout": "imdb",
                "date_mode": "now",
            },
        )
        unwatched = self.client.post(
            "/search/unwatch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "scope": "season",
                "season": 2,
                "season_layout": "imdb",
            },
        )

        self.assertEqual(watched.status_code, 200)
        self.assertEqual(self.search_watch.mark_calls[-1]["season_layout"], "imdb")
        self.assertEqual(unwatched.status_code, 200)
        self.assertEqual(self.search_watch.unmark_scope_calls[-1]["season_layout"], "imdb")

        invalid = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "scope": "season",
                "season": 2,
                "season_layout": "client-episodes",
                "date_mode": "now",
            },
        )
        self.assertEqual(invalid.status_code, 400)

    def test_search_restore_watch_accepts_scope_payload(self) -> None:
        response = self.client.post(
            "/search/restore-watch",
            json={
                "restore": {
                    "kind": "scope",
                    "title_type": "movie",
                    "trakt_id": 1,
                    "items": [
                        {
                            "title_type": "movie",
                            "trakt_id": 1,
                            "title": "Movie A",
                            "watched_at": "2026-07-01T12:00:00+00:00",
                            "watched_at_known": True,
                        }
                    ],
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.search_watch.restore_scope_calls[0][0]["watched_at"],
            datetime(2026, 7, 1, 12, tzinfo=UTC),
        )

    def test_search_restore_watch_rejects_unknown_date(self) -> None:
        response = self.client.post(
            "/search/restore-watch",
            json={
                "restore": {
                    "trakt_id": 3,
                    "title": "The Capture",
                    "season": 1,
                    "episode": 1,
                    "watched_at": "2026-07-01T12:00:00+00:00",
                    "watched_at_known": False,
                }
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.search_watch.restore_calls, [])
        script = (STATIC_DIR / "show_watch_panel.js").read_text(encoding="utf-8")
        self.assertIn("restore.can_restore !== false", script)

    def test_search_show_watch_panel_returns_stills_from_completed_enrichment(self) -> None:
        self.search_watch.return_still_after_enrich = True

        response = self.client.get("/search/show/3/watch-panel?refresh=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.enrich_still_calls, [(3, 1)])
        self.assertEqual(self.enrich_tasks, [])
        self.assertIn("no-still.jpg", response.text)

    def test_search_show_watch_panel_artwork_patch_omits_full_episode_panel(self) -> None:
        self.search_watch.return_still_after_enrich = True

        response = self.client.get("/search/show/3/watch-panel?refresh=1&artwork_patch=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.enrich_still_calls, [(3, 1)])
        self.assertIn('data-episode-key="1-1"', response.text)
        self.assertIn("no-still.jpg", response.text)
        self.assertNotIn("search-watch-episode-card", response.text)
        self.assertNotIn("data-search-watch-panel", response.text)

    def test_search_show_watch_panel_returns_cold_episode_list_before_still_enrichment(self) -> None:
        self.search_watch.episodes_hydrated = False
        self.search_watch.return_still_after_enrich = True
        self.search_watch.pending_still_before_enrich = True

        initial = self.client.get("/search/show/3/watch-panel")
        episodes_completed = self.client.get("/search/show/3/watch-panel?refresh=1")

        self.assertEqual(initial.status_code, 200)
        self.assertIn('data-watch-panel-pending="1"', initial.text)
        self.assertEqual(episodes_completed.status_code, 200)
        self.assertEqual(self.enrich_tasks, [])
        self.assertEqual(self.search_watch.hydrate_calls, [3])
        self.assertEqual(self.search_watch.enrich_still_calls, [])
        self.assertIn("Pilot", episodes_completed.text)
        self.assertNotIn('data-watch-panel-pending="1"', episodes_completed.text)
        still_tasks = [
            kwargs
            for args, kwargs in self.bg_task_calls
            if args and args[0] == "search_enrichment_watch_panel_stills_3_1"
        ]
        self.assertEqual(len(still_tasks), 1)
        selected_season = episodes_completed.text.split('data-search-watch-season-panel="1"', 1)[1]
        self.assertIn('data-still-pending="1"', selected_season)

        stills_completed = self.client.get("/search/show/3/watch-panel?refresh=1")

        self.assertEqual(stills_completed.status_code, 200)
        self.assertEqual(self.search_watch.enrich_still_calls, [(3, 1)])
        self.assertIn("no-still.jpg", stills_completed.text)
        selected_season = stills_completed.text.split('data-search-watch-season-panel="1"', 1)[1]
        self.assertNotIn('data-still-pending="1"', selected_season)

    def test_search_show_watch_panel_refresh_ends_empty_episode_state(self) -> None:
        self.search_watch.episodes_hydrated = False
        self.search_watch.hydrate_result = False

        response = self.client.get("/search/show/3/watch-panel?refresh=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.hydrate_calls, [3])
        self.assertIn("No episodes found.", response.text)
        self.assertNotIn('data-watch-panel-pending="1"', response.text)

    def test_search_show_watch_panel_refresh_targets_requested_season(self) -> None:
        response = self.client.get("/search/show/3/watch-panel?season=0&refresh=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.hydrate_calls, [])
        self.assertEqual(self.search_watch.enrich_still_calls, [(3, 0)])
        self.assertTrue(all(call["default_season"] == 0 for call in self.search_watch.load_calls))

    def test_search_show_watch_panel_refresh_does_not_wait_for_imdb_mapping(self) -> None:
        self.search_watch.mapping_pending = True
        self.search_watch.return_still_after_enrich = True

        response = self.client.get("/search/show/3/watch-panel?refresh=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.enrich_still_calls, [(3, 1)])
        self.assertIn("no-still.jpg", response.text)
        self.assertIn('data-imdb-mapping-pending="1"', response.text)

    def test_watch_panel_clients_share_the_completion_contract(self) -> None:
        shared = (STATIC_DIR / "show_watch_panel.js").read_text(encoding="utf-8")
        self.assertIn("function needsRefresh(body)", shared)
        self.assertIn("body?.querySelector(\"[data-watch-panel-pending='1']\")", shared)
        self.assertIn("function applyRefresh(body, html)", shared)
        self.assertIn("body.replaceChildren(", shared)

        for script_name in (
            "catalog_page.js",
            "history_watch_panel.js",
            "release_tracking_page.js",
        ):
            script = (STATIC_DIR / script_name).read_text(encoding="utf-8")
            self.assertIn("traktShowWatchPanel?.needsRefresh(watchBody)", script, script_name)
            self.assertIn('refreshUrl.searchParams.set("refresh", "1")', script, script_name)
            self.assertIn('refreshUrl.searchParams.set("artwork_patch", "1")', script, script_name)
            self.assertIn("traktShowWatchPanel?.applyRefresh(watchBody, html)", script, script_name)
            self.assertIn("let needsArtworkFollowup = false;", script, script_name)
            self.assertIn("needsArtworkFollowup = Boolean(", script, script_name)
            self.assertIn(
                "void refreshWatchPanel(requestUrl, token, {state, focusDefault});",
                script,
                script_name,
            )
            self.assertIn("await refreshWatchPanel(requestUrl, token, {state, focusDefault});", script, script_name)
            self.assertNotIn(
                'if (watchBody.querySelector("[data-search-watch-panel]")?.dataset.imdbMappingPending !== "1")',
                script,
                script_name,
            )

        history_script = (STATIC_DIR / "history_watch_panel.js").read_text(encoding="utf-8")
        self.assertNotIn("setTimeout(() => refreshWatchPanel", history_script)

    def test_search_show_watch_panel_schedules_still_warm_without_blocking_response(self) -> None:
        response = self.client.get("/search/show/3/watch-panel")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pilot", response.text)
        self.assertEqual(len(self.image_tasks), 1)

    def test_search_show_watch_panel_queues_incomplete_imdb_mapping(self) -> None:
        self.search_watch.mapping_pending = True

        response = self.client.get("/search/show/3/watch-panel")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-imdb-mapping-pending="1"', response.text)
        self.assertIn("Completing IMDb mapping", response.text)
        mapping_tasks = [
            kwargs
            for args, kwargs in self.bg_task_calls
            if args and args[0] == "imdb_watch_panel_3"
        ]
        self.assertEqual(len(mapping_tasks), 1)
        mapping_tasks[0]["fn"]()
        self.assertEqual(self.search_watch.repair_calls, [3])

    def test_search_watch_post_rejects_undated_movie(self) -> None:
        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "movie",
                "trakt_id": 2,
                "title": "Movie B",
                "scope": "title",
                "date_mode": "none",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self.search_watch.mark_calls, [])

    def test_search_watch_post_parses_custom_date(self) -> None:
        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "title": "The Capture",
                "scope": "episode",
                "season": "1",
                "episode": "1",
                "date_mode": "custom",
                "watched_at": "2026-04-01T20:30",
            },
        )
        self.assertEqual(response.status_code, 200)
        call = self.search_watch.mark_calls[-1]
        self.assertEqual(call["season"], 1)
        self.assertEqual(call["episode"], 1)
        self.assertEqual(call["season_layout"], "trakt")
        self.assertEqual(call["watched_at"].astimezone(UTC).hour, 17)
        progress_tasks = [
            kwargs
            for args, kwargs in self.bg_task_calls
            if args and args[0] == "progress_refresh_after_watch_3"
        ]
        self.assertEqual(len(progress_tasks), 1)
        progress_tasks[0]["fn"]()
        self.assertEqual(self.progress_refresh_calls, [(3, True, False)])

    def test_watchlist_title_watch_removes_it_from_watchlist(self) -> None:
        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "movie",
                "trakt_id": 4,
                "title": "Arrival",
                "scope": "title",
                "date_mode": "now",
                "remove_from_watchlist": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["removed_from_watchlist"])
        self.assertEqual(self.watchlist_calls, [])
        tasks = {args[0]: kwargs for args, kwargs in self.bg_task_calls if args}
        self.assertIn("watchlist_remove_after_watch_movie_4", tasks)
        tasks["watchlist_remove_after_watch_movie_4"]["fn"]()
        self.assertEqual(
            self.watchlist_calls[-1],
            {"title_type": "movie", "trakt_id": 4, "watchlisted": False},
        )

    def test_show_episode_watch_removes_watchlisted_title(self) -> None:
        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "title": "The Capture",
                "scope": "episode",
                "season": 1,
                "episode": 1,
                "date_mode": "now",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["removed_from_watchlist"])
        self.assertEqual(self.watchlist_calls, [])
        tasks = {args[0]: kwargs for args, kwargs in self.bg_task_calls if args}
        self.assertIn("watchlist_remove_after_watch_show_3", tasks)
        self.assertIn("progress_refresh_after_watch_3", tasks)
        tasks["watchlist_remove_after_watch_show_3"]["fn"]()
        self.assertEqual(
            self.watchlist_calls,
            [{"title_type": "show", "trakt_id": 3, "watchlisted": False}],
        )

    def test_show_season_watch_removes_watchlisted_title(self) -> None:
        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "title": "The Capture",
                "scope": "season",
                "season": 1,
                "season_layout": "imdb",
                "date_mode": "now",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["removed_from_watchlist"])
        self.assertEqual(self.watchlist_calls, [])
        tasks = {args[0]: kwargs for args, kwargs in self.bg_task_calls if args}
        self.assertIn("watchlist_remove_after_watch_show_3", tasks)
        self.assertIn("progress_refresh_after_watch_3", tasks)
        tasks["watchlist_remove_after_watch_show_3"]["fn"]()
        self.assertEqual(
            self.watchlist_calls,
            [{"title_type": "show", "trakt_id": 3, "watchlisted": False}],
        )

    def test_title_watch_queues_release_tracking_cleanup(self) -> None:
        self.release_tracking_keys.add(("show", 3))

        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 3,
                "title": "The Capture",
                "scope": "title",
                "date_mode": "now",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.release_tracking_calls, [])
        tasks = {args[0]: kwargs for args, kwargs in self.bg_task_calls if args}
        self.assertIn("release_remove_after_watch_show_3", tasks)
        tasks["release_remove_after_watch_show_3"]["fn"]()
        self.assertEqual(
            self.release_tracking_calls,
            [{"title_type": "show", "trakt_id": 3, "tracked": False}],
        )

    def test_show_watch_does_not_write_watchlist_when_title_is_not_listed(self) -> None:
        response = self.client.post(
            "/search/watch",
            json={
                "title_type": "show",
                "trakt_id": 30,
                "title": "Not Listed",
                "scope": "episode",
                "season": 1,
                "episode": 1,
                "date_mode": "now",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["removed_from_watchlist"])
        self.assertEqual(self.watchlist_calls, [])
        self.assertEqual(
            [args[0] for args, _kwargs in self.bg_task_calls if args],
            ["progress_refresh_after_watch_30"],
        )


if __name__ == "__main__":
    unittest.main()
