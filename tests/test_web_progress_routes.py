from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trakt_tracker.application.enrich_queue import TASK_STATUS_COMPLETED
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot
from trakt_tracker.web.routes_progress import register_progress_routes
from trakt_tracker.web.viewmodels import (
    progress_effective_aired,
    progress_effective_percent,
    progress_query_string,
    progress_rating_chip,
    progress_recent_release,
    progress_skipped_count,
)


class _FakeProgressService:
    def __init__(self, items: list[ProgressSnapshot]) -> None:
        self.items = items

    def dashboard_progress(self, dropped_only: bool = False):
        if dropped_only:
            return [item for item in self.items if item.is_dropped]
        return [item for item in self.items if not item.is_dropped]

    def select_title_enrich_keys(self, items):
        result = []
        for item in items:
            if item.poster_status in {"unknown", "retryable_failure"} or item.title_ratings_status in {"unknown", "retryable_failure"}:
                result.append((int(item.trakt_id), "show"))
        return result

    def select_episode_enrich_keys(self, items):
        result = []
        for item in items:
            if item.next_episode is None:
                continue
            if (
                item.next_episode.still_status in {"unknown", "retryable_failure"}
                or item.next_episode.trakt_details_status in {"unknown", "retryable_failure"}
                or item.next_episode.imdb_status in {"unknown", "retryable_failure"}
            ):
                result.append((int(item.trakt_id), int(item.next_episode.season), int(item.next_episode.number)))
        return result

    def sync_progress(self, trakt_ids=None, dropped_only=False):
        return []


class _FakeEnrichQueueService:
    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.revision = 1
        self.running = False
        self.updates = [
            {
                "revision": 1,
                "task_key": "title:show:1",
                "kind": "progress_title",
                "status": TASK_STATUS_COMPLETED,
                "result": "ready",
                "affected_title_keys": ["progress:1"],
            }
        ]

    def submit_progress_refresh(self, *, viewport_tasks, nearby_tasks, page_tasks):
        self.submissions.append(
            {
                "viewport": list(viewport_tasks),
                "nearby": list(nearby_tasks),
                "page": list(page_tasks),
            }
        )
        return self.revision

    def list_updates(self, after_revision=0, relevant_title_keys=None):
        keys = set(relevant_title_keys or set())
        updates = [
            update
            for update in self.updates
            if update["revision"] > after_revision
            and (not keys or any(key in keys for key in update["affected_title_keys"]))
        ]
        return {"revision": self.revision, "running": self.running, "updates": updates}

    def is_running(self, relevant_title_keys=None):
        return self.running


class _FakeBackgroundTaskManager:
    def __init__(self) -> None:
        self.running: set[str] = set()

    def is_running(self, key: str) -> bool:
        return key in self.running

    def start(self, key: str, *, source: str, operations, fn) -> bool:
        self.running.add(key)
        return True


class ProgressRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/templates")
        static_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/static")
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["dt"] = lambda value: value.isoformat() if value else ""
        self.templates.env.filters["episode_label"] = lambda season, episode: f"S{int(season):02d}E{int(episode):02d}" if season is not None and episode is not None else ""
        self.templates.env.filters["cached_image_url"] = lambda value: value or ""
        self.templates.env.filters["progress_effective_aired"] = progress_effective_aired
        self.templates.env.filters["progress_effective_percent"] = progress_effective_percent
        self.templates.env.filters["progress_skipped_count"] = progress_skipped_count
        self.templates.env.filters["progress_recent_release"] = progress_recent_release
        self.templates.env.filters["progress_rating_chip"] = lambda item: progress_rating_chip(item, lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a")
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        items = [
            ProgressSnapshot(
                trakt_id=1,
                title="Severance",
                completed=1,
                aired=2,
                percent_completed=50.0,
                next_episode=EpisodeSummary(
                    trakt_id=11,
                    season=2,
                    number=3,
                    title="Who Is Alive?",
                    still_status="unknown",
                    trakt_details_status="unknown",
                    imdb_status="unknown",
                ),
                poster_status="unknown",
                title_ratings_status="unknown",
            ),
            ProgressSnapshot(
                trakt_id=2,
                title="Andor",
                completed=1,
                aired=2,
                percent_completed=50.0,
                next_episode=EpisodeSummary(
                    trakt_id=12,
                    season=1,
                    number=4,
                    title="Aldhani",
                    still_status="checked_no_data",
                    trakt_details_status="checked_no_data",
                    imdb_status="checked_no_data",
                ),
                poster_status="checked_no_data",
                title_ratings_status="checked_no_data",
            ),
        ]
        self.progress = _FakeProgressService(items)
        self.queue = _FakeEnrichQueueService()
        self.app.state.services = SimpleNamespace(
            progress=self.progress,
            enrich_queue=self.queue,
            notifications=SimpleNamespace(unseen_episode_ids=lambda: set()),
            auth=SimpleNamespace(
                config=SimpleNamespace(
                    hide_upcoming_in_progress=False,
                    show_dropped_in_progress=False,
                    web_progress_min_year=None,
                    web_progress_year_filter_enabled=False,
                ),
            ),
            interactions=SimpleNamespace(),
            play=SimpleNamespace(),
            operations=SimpleNamespace(),
        )
        self.app.state.bg_tasks = _FakeBackgroundTaskManager()

        def render(request: Request, template_name: str, context: dict, status_code: int = 200) -> HTMLResponse:
            base_context = {
                "request": request,
                "current_path": request.url.path,
                "authorized": True,
                "configured": True,
                "settings_utc_offset": "+03:00",
                "debug_mode": False,
                "debug_initial_seq": 0,
            }
            base_context.update(context)
            return self.templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

        def progress_redirect(**kwargs) -> RedirectResponse:
            return RedirectResponse(url=f"/progress?{progress_query_string(**kwargs)}", status_code=303)

        self.app.state.render_fragment = lambda request, template_name, context: self.templates.get_template(template_name).render(
            {"request": request, "current_path": request.url.path, **context}
        )
        register_progress_routes(self.app, render=render, progress_redirect=progress_redirect)
        self.client = TestClient(self.app)

    def test_progress_refresh_returns_only_requested_cards(self) -> None:
        response = self.client.post(
            "/progress/refresh",
            json={
                "hide_upcoming": "0",
                "show_dropped": "0",
                "min_year": "",
                "use_year_filter": "0",
                "viewport_card_keys": ["progress:1"],
                "nearby_card_keys": [],
                "page_card_keys": ["progress:1", "progress:2"],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["card_key"] for item in payload["cards"]], ["progress:1"])
        self.assertEqual(len(self.queue.submissions), 1)
        self.assertEqual([task.task_key for task in self.queue.submissions[0]["viewport"]], ["title:show:1", "episode:1:2:3"])

    def test_progress_template_renders_loading_and_terminal_empty_states(self) -> None:
        response = self.client.get("/progress")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("poster-loading", html)
        self.assertIn("progress-episode-preview", html)
        self.assertIn("Loading", html)
        self.assertIn("No poster", html)
        self.assertIn("No preview", html)
        self.assertGreaterEqual(html.count("n/a"), 2)


if __name__ == "__main__":
    unittest.main()
