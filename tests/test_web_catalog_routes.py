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
import trakt_tracker.web.routes_catalog as routes_catalog
from trakt_tracker.domain import TitleSummary
from trakt_tracker.application.search_watch import SearchShowWatchPanel, SearchWatchEpisode, SearchWatchSeason


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
    ) -> EpisodeRatingsMatrixViewModel:
        self.calls.append(
            {
                "trakt_id": trakt_id,
                "force_refresh": force_refresh,
                "provider": provider,
                "refresh_missing": refresh_missing,
            }
        )
        return EpisodeRatingsMatrixViewModel(
            trakt_id=trakt_id,
            title="The Capture",
            subtitle=("Trakt episode ratings by season" if provider == "trakt" else "IMDb episode ratings by season"),
            legend=[EpisodeMatrixLegendItem(label="Awesome", threshold_label=">= 9", color="rgb(24, 106, 59)")],
            seasons=[EpisodeMatrixSeason(season=1, label="S1", avg_display="8.3", avg_rating=8.3, avg_color="rgb(40, 180, 99)")],
            rows=[
                EpisodeMatrixRow(
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
            ],
            has_episodes=True,
            provider=("trakt" if provider == "trakt" else "imdb"),
        )


class _FakeSearchWatchService:
    def __init__(self) -> None:
        self.mark_calls: list[dict] = []

    def load_show_panel(self, trakt_id: int) -> SearchShowWatchPanel:
        return SearchShowWatchPanel(
            trakt_id=trakt_id,
            title="The Capture",
            poster_url="https://poster.example/capture.jpg",
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
                            title="Pilot",
                            still_url="https://still.example/pilot.jpg",
                            trakt_rating=8.1,
                            trakt_votes=100,
                            imdb_rating=8.3,
                            imdb_votes=120,
                            first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                        ),
                        SearchWatchEpisode(
                            season=1,
                            number=2,
                            title="No Still",
                            still_status="retryable_failure",
                            first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                        )
                    ],
                ),
            ],
        )

    def mark_watch(self, **kwargs) -> int:
        self.mark_calls.append(kwargs)
        return 3 if kwargs.get("scope") == "title" else 1


class CatalogRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/templates")
        static_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/static")
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["rating_with_votes"] = lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a"
        self.templates.env.filters["cached_image_url"] = lambda value: (f"/cached-image?url={quote(str(value))}&v=3" if value else "")
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.matrix = _FakeEpisodeRatingsMatrixService()
        self.search_watch = _FakeSearchWatchService()
        self.app.state.services = SimpleNamespace(
            catalog=SimpleNamespace(
                load_last_search_state=lambda: None,
                get_search_sort_mode=lambda: "IMDb votes",
                set_search_sort_mode=lambda value: value,
                search_history=lambda: [],
                get_title_details=lambda trakt_id, title_type: TitleSummary(trakt_id=trakt_id, title_type=title_type, title="Fallback"),
                search_titles=lambda query, title_type=None: [
                    TitleSummary(
                        trakt_id=1,
                        title_type="movie",
                        title="Movie A",
                        poster_url="https://poster.example/movie-a.jpg",
                        trakt_rating=7.0,
                        trakt_votes=10,
                        ratings_status=ENRICH_STATUS_CHECKED_NO_DATA,
                    ),
                    TitleSummary(trakt_id=2, title_type="movie", title="Movie B", trakt_rating=6.0, trakt_votes=8),
                    TitleSummary(
                        trakt_id=3,
                        title_type="show",
                        title="The Capture",
                        poster_url="https://poster.example/capture.jpg",
                        trakt_rating=8.0,
                        trakt_votes=80,
                    ),
                ],
            ),
            episode_ratings_matrix=self.matrix,
            search_watch=self.search_watch,
            history=SimpleNamespace(title_rating_badges=lambda trakt_ids: {1: 9.0, 3: 8.5}),
            play=SimpleNamespace(resolve_kinopoisk_url=lambda title: f"https://kino.example/{quote(title)}" if title else None),
            operations=SimpleNamespace(publish=lambda *_args, **_kwargs: None),
            auth=SimpleNamespace(config=SimpleNamespace(utc_offset="+03:00")),
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
            schedule_search_enrichment=lambda *args, **kwargs: False,
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
                "refresh_missing": True,
            },
        )

    def test_search_page_renders_without_waiting_for_metadata_enrichment(self) -> None:
        response = self.client.get("/search?q=test&type=all&sort=IMDb+votes")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Loading", html)
        self.assertIn("n/a", html)
        self.assertLess(html.index("Movie B"), html.index("Movie A"))

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
        self.assertIn('/search/movie/1/play?title=Movie%20A', html)
        self.assertIn('/search/show/3/play?title=The%20Capture', html)
        self.assertIn('8.5 &#9733;', html)
        self.assertIn('9.0 &#9733;', html)
        self.assertIn('/cached-image?url=https%3A//poster.example/capture.jpg&amp;v=3', html)
        self.assertIn('/cached-image?url=https%3A//poster.example/movie-a.jpg&amp;v=3', html)
        self.assertIn("proxyRetryApplied", html)
        self.assertNotIn("data-direct-src", html)
        self.assertNotIn("dataset.directSrc", html)

    def test_search_show_watch_panel_fragment_renders_default_season_cards(self) -> None:
        response = self.client.get("/search/show/3/watch-panel")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-search-watch-season-tab="1"', html)
        self.assertIn('data-search-watch-season-tab="0"', html)
        self.assertIn('data-search-watch-season-panel="1"', html)
        self.assertIn("Pilot", html)
        self.assertIn("No preview", html)
        self.assertIn("Mark season", html)
        self.assertIn("Mark series", html)
        self.assertIn('/cached-image?url=https%3A//still.example/pilot.jpg&amp;v=3', html)
        self.assertIn("proxyRetryApplied", html)
        self.assertNotIn("data-direct-src", html)
        self.assertNotIn("dataset.directSrc", html)

    def test_search_show_watch_panel_schedules_still_warm_without_blocking_response(self) -> None:
        starts: list[object] = []

        class _FakeThread:
            def __init__(self, *, target, daemon) -> None:
                self.target = target
                self.daemon = daemon

            def start(self) -> None:
                starts.append(self.target)

        original_thread = routes_catalog.Thread
        self.app.state.image_cache = object()
        routes_catalog.Thread = _FakeThread
        try:
            response = self.client.get("/search/show/3/watch-panel")
        finally:
            routes_catalog.Thread = original_thread

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pilot", response.text)
        self.assertEqual(len(starts), 1)

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


if __name__ == "__main__":
    unittest.main()
