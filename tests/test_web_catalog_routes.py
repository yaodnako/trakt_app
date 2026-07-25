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
)
from trakt_tracker.application.enrich_state import ENRICH_STATUS_CHECKED_NO_DATA
from trakt_tracker.web.routes_catalog import register_catalog_routes
from trakt_tracker.domain import ExploreResultPage, TitleSummary
from trakt_tracker.application.search_watch import SearchShowWatchPanel, SearchWatchEpisode, SearchWatchSeason

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "trakt_tracker" / "web" / "static"


class _FakeEpisodeRatingsMatrixService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

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
            has_episodes=True,
            provider=("trakt" if provider == "trakt" else "imdb"),
        )

    def select_trakt_rating_refresh_keys(self, trakt_id: int, *, force_refresh: bool = False) -> list[tuple[int, int]]:
        return [(1, 1)]


class _FakeSearchWatchService:
    def __init__(self) -> None:
        self.mark_calls: list[dict] = []
        self.enrich_still_calls: list[tuple[int, int]] = []
        self.return_still_after_enrich = False
        self.unmark_calls: list[dict] = []
        self.restore_calls: list[dict] = []
        self.unmark_scope_calls: list[dict] = []
        self.restore_scope_calls: list[list[dict]] = []

    def load_show_panel(self, trakt_id: int, default_season: int | None = None) -> SearchShowWatchPanel:
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
                    is_default=True,
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
                                else "checked_no_data"
                            ),
                            first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                            is_watched=True,
                        )
                    ],
                ),
            ],
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


class CatalogRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        static_dir = STATIC_DIR
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["rating_with_votes"] = lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a"
        self.templates.env.filters["compact_votes"] = lambda value: f"{value / 1000:.1f}k" if value >= 1000 else str(value)
        self.templates.env.filters["cached_image_url"] = lambda value: (f"/cached-image?url={quote(str(value))}&v=3" if value else "")
        self.templates.env.filters["episode_preview_url"] = lambda value: str(value).replace("/w780/", "/w342/")
        self.templates.env.filters["dt"] = lambda value: value.isoformat() if value else ""
        self.templates.env.filters["release_distance"] = lambda value: "soon" if value else "Release date unknown"
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.matrix = _FakeEpisodeRatingsMatrixService()
        self.search_watch = _FakeSearchWatchService()
        self.watchlist_calls: list[dict] = []
        self.catalog_watchlist_keys = {("show", 3)}
        self.release_tracking_keys = set()
        self.catalog_history_keys = {("movie", 1)}
        self.enrichment_calls: list[dict] = []
        self.enrich_tasks: list[object] = []
        self.image_tasks: list[tuple[str, int]] = []
        self.progress_refresh_calls: list[tuple[int, bool]] = []
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
                set_watchlisted=lambda title_type, trakt_id, *, watchlisted: self.watchlist_calls.append(
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
                refresh_show_progress=lambda trakt_id, *, fresh=False: self.progress_refresh_calls.append(
                    (trakt_id, fresh)
                )
            ),
            play=SimpleNamespace(resolve_kinopoisk_url=lambda title: f"https://kino.example/{quote(title)}" if title else None),
            operations=SimpleNamespace(publish=lambda *_args, **_kwargs: None),
            release_tracking=SimpleNamespace(
                keys=lambda: set(self.release_tracking_keys),
                local_keys=lambda: set(self.release_tracking_keys),
                local_items=lambda: list(self.release_items),
                refresh=lambda: list(self.release_items),
                refresh_anticipated_list_counts=lambda _items: None,
            ),
            auth=SimpleNamespace(config=SimpleNamespace(utc_offset="+03:00", explore_imdb_scan_page_limit=10)),
        )
        self.app.state.bg_tasks = SimpleNamespace(
            start=lambda *args, **kwargs: self.bg_task_calls.append((args, kwargs)) or True
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
            }
            base_context.update(context)
            return self.templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

        def render_fragment(request: Request, template_name: str, context: dict) -> str:
            fragment_context = {"request": request, "current_path": request.url.path}
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
        self.assertIn('id="release-watch-overlay"', html)
        self.assertIn("data-show-watch-play", html)
        release_script = (STATIC_DIR / "release_tracking_page.js").read_text(encoding="utf-8")
        self.assertIn("configurePlayAction(watchOverlay, trigger)", release_script)
        self.assertIn("show_watch_panel.js?v=", html)
        self.assertIn('data-title-matrix-url="/titles/show/21/episode-ratings-matrix"', html)
        self.assertIn('data-title-matrix-url="/titles/show/22/episode-ratings-matrix"', html)
        self.assertNotIn("scheduleWatchPanelRefreshIfPending", html)
        self.assertIn("refreshWatchPanel", release_script)
        self.assertEqual(html.count('loading="lazy" decoding="async" fetchpriority="low"'), 1)

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
        self.assertLess(html.index("data-search-watch-header-mark"), html.index("data-search-watch-header-unwatch"))
        self.assertLess(html.index("data-search-watch-header-unwatch"), html.index("data-show-watch-play"))
        self.assertIn('/search/movie/1/play?title=Movie%20A', html)
        self.assertIn('/search/show/3/play?title=The%20Capture', html)
        self.assertIn('8.5 &#9733;', html)
        self.assertIn('9.0 &#9733;', html)
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
            "history_show_watch_overlay.html",
        ):
            self.assertNotIn("<script>", (templates / name).read_text(encoding="utf-8"), name)
        self.assertTrue((STATIC_DIR / "ui_core.js").is_file())
        self.assertTrue((STATIC_DIR / "catalog_page.js").is_file())

    def test_partial_catalog_navigation_replaces_active_navigation(self) -> None:
        script = (STATIC_DIR / "catalog_page.js").read_text(encoding="utf-8")

        self.assertIn("setActiveNavigation(target.pathname)", script)
        self.assertIn('const incomingNav = parsed.querySelector(".nav")', script)
        self.assertIn('const currentNav = document.querySelector(".nav")', script)
        self.assertIn("currentNav.replaceWith(incomingNav)", script)

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
        self.assertIn("S01E01 (S02E01)", html)
        self.assertIn("No preview", html)
        self.assertIn('data-still-pending="1"', html)
        self.assertIn('title="Mark all released episodes in S0 watched"', html)
        self.assertIn('title="Remove watched history for S1"', html)
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
        self.assertIn('data-scope="season"', html)
        self.assertNotIn("Mark season", html)
        self.assertNotIn("Undo season", html)
        self.assertNotIn("Undo entire series", html)
        self.assertIn("seen.svg", html)
        self.assertIn("cancel.svg", html)
        self.assertIn('class="history-rating-badge search-watch-user-rating"', html)
        self.assertIn('data-rating-season="1"', html)
        self.assertIn('data-rating-episode="1"', html)
        self.assertIn("8 &#9733;", html)
        self.assertIn('class="history-rate-chip search-watch-user-rating"', html)
        self.assertIn(">Rate</button>", html)
        page = self.client.get("/search?q=test&type=all")
        self.assertIn("data-search-watch-title-ratings", page.text)
        self.assertIn("data-title-matrix-title-ratings", page.text)
        panel_script = (STATIC_DIR / "show_watch_panel.js").read_text(encoding="utf-8")
        self.assertIn("configureTitleRatings(overlay, body)", panel_script)
        self.assertIn("episode-ratings-matrix", panel_script)
        self.assertIn("syncTitleMatrixTitleRatings", ui_script)

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
            mark_all_index = template.index("data-search-watch-header-mark")
            self.assertLess(actions_index, ratings_index, template_path.name)
            self.assertLess(ratings_index, mark_all_index, template_path.name)

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
            [{"title_type": "movie", "trakt_id": 1, "scope": "title", "season": None}],
        )
        self.assertFalse(response.json()["still_watched"])

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

    def test_search_restore_watch_preserves_restore_metadata(self) -> None:
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

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.search_watch.restore_calls[0]["watched_at_known"])
        self.assertEqual(self.search_watch.restore_calls[0]["watched_at"], datetime(2026, 7, 1, 12, tzinfo=UTC))

    def test_search_show_watch_panel_returns_stills_from_completed_enrichment(self) -> None:
        self.search_watch.return_still_after_enrich = True

        response = self.client.get("/search/show/3/watch-panel?refresh=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search_watch.enrich_still_calls, [])
        self.assertTrue(self.enrich_tasks)
        self.assertNotIn("no-still.jpg", response.text)

    def test_search_show_watch_panel_schedules_still_warm_without_blocking_response(self) -> None:
        response = self.client.get("/search/show/3/watch-panel")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pilot", response.text)
        self.assertEqual(len(self.image_tasks), 1)

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
        self.assertEqual(call["watched_at"].astimezone(UTC).hour, 17)
        progress_tasks = [
            kwargs
            for args, kwargs in self.bg_task_calls
            if args and args[0] == "progress_watch_3_episode_1_1"
        ]
        self.assertEqual(len(progress_tasks), 1)
        progress_tasks[0]["fn"]()
        self.assertEqual(self.progress_refresh_calls, [(3, True)])

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
        self.assertEqual(
            self.watchlist_calls[-1],
            {"title_type": "movie", "trakt_id": 4, "watchlisted": False},
        )


if __name__ == "__main__":
    unittest.main()
