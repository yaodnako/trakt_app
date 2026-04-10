from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

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
from trakt_tracker.web.routes_catalog import register_catalog_routes
from trakt_tracker.domain import TitleSummary


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


class CatalogRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/templates")
        static_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/static")
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["rating_with_votes"] = lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a"
        self.templates.env.filters["cached_image_url"] = lambda value: value or ""
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.matrix = _FakeEpisodeRatingsMatrixService()
        self.app.state.services = SimpleNamespace(
            catalog=SimpleNamespace(
                load_last_search_state=lambda: None,
                get_search_sort_mode=lambda: "IMDb votes",
                set_search_sort_mode=lambda value: value,
                search_history=lambda: [],
                search_titles=lambda query, title_type=None: [
                    TitleSummary(trakt_id=1, title_type="movie", title="Movie A", trakt_rating=7.0, trakt_votes=10),
                    TitleSummary(trakt_id=2, title_type="movie", title="Movie B", trakt_rating=6.0, trakt_votes=8),
                ],
            ),
            episode_ratings_matrix=self.matrix,
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
            enrich_search_results=lambda services, results, **kwargs: (
                [
                    TitleSummary(
                        trakt_id=item.trakt_id,
                        title_type=item.title_type,
                        title=item.title,
                        trakt_rating=item.trakt_rating,
                        trakt_votes=item.trakt_votes,
                        imdb_rating=(9.5 if item.trakt_id == 2 else 7.1),
                        imdb_votes=(500 if item.trakt_id == 2 else 100),
                    )
                    for item in results
                ],
                True,
            ),
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
        self.assertIn('data-title-matrix-provider-select', html)
        self.assertIn('<option value="imdb" selected>IMDb</option>', html)
        self.assertIn('<option value="trakt" >Trakt</option>', html)
        self.assertIn('data-my-rating-toggle', html)

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

    def test_search_page_renders_synchronously_enriched_imdb_values(self) -> None:
        response = self.client.get("/search?q=test&type=all&sort=IMDb+votes")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("9.5 (500)", html)
        self.assertIn("7.1 (100)", html)
        self.assertLess(html.index("Movie B"), html.index("Movie A"))


if __name__ == "__main__":
    unittest.main()
