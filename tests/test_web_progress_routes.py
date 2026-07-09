from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trakt_tracker.application.enrich_queue import TASK_STATUS_COMPLETED
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_STILL,
    TRIGGER_PAGE_CONTEXT,
    metadata_refresh_due,
)
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot
from trakt_tracker.web.routes_progress import register_progress_routes
from trakt_tracker.web.viewmodels import (
    progress_effective_aired,
    progress_effective_percent,
    progress_episode_rating_chip,
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

    def select_title_enrich_keys(self, items, *, trigger="viewport", requested_parts=(), refresh_requests=None):
        result = []
        for item in items:
            needs_poster = item.poster_status in {"unknown", "retryable_failure"}
            needs_ratings = item.title_ratings_status in {"unknown", "retryable_failure"} or (
                requested_parts and "title_ratings" in requested_parts and item.title_ratings_status == "ready"
            )
            if needs_poster or needs_ratings:
                result.append((int(item.trakt_id), "show"))
        return result

    def select_episode_enrich_keys(self, items, *, trigger="viewport", requested_parts=(), refresh_requests=None):
        result = []
        for item in items:
            if item.next_episode is None:
                continue
            wants_still = not requested_parts or "still" in requested_parts
            wants_ratings = not requested_parts or "episode_ratings" in requested_parts
            still_due = wants_still and metadata_refresh_due(
                ASSET_KIND_STILL,
                status=item.next_episode.still_status,
                last_checked_at=getattr(item.next_episode, "still_refreshed_at", None),
                has_value=bool(getattr(item.next_episode, "still_url", "")),
                trigger=trigger,
                first_aired=getattr(item.next_episode, "first_aired", None),
            ).should_refresh
            ratings_due = wants_ratings and (
                item.next_episode.trakt_details_status in {"unknown", "retryable_failure"}
                or (requested_parts and "episode_ratings" in requested_parts and item.next_episode.trakt_details_status == "ready")
                or item.next_episode.imdb_status in {"unknown", "retryable_failure"}
            )
            if still_due or ratings_due:
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
        self.templates.env.filters["progress_episode_rating_chip"] = lambda item: progress_episode_rating_chip(item, lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a")
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
        self.unseen_episode_ids: set[int] = set()
        self.app.state.services = SimpleNamespace(
            progress=self.progress,
            enrich_queue=self.queue,
            notifications=SimpleNamespace(unseen_episode_ids=lambda: self.unseen_episode_ids),
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
                "notification_sound_url": "",
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
        self.assertIn('data-title-matrix-url="/titles/show/1/episode-ratings-matrix"', html)
        self.assertIn("Loading", html)
        self.assertIn("No poster", html)
        self.assertIn("No preview", html)
        self.assertGreaterEqual(html.count("n/a"), 2)
        self.assertNotIn(">New</h3>", html)

    def test_progress_refresh_queues_ratings_only_requests_for_stale_visible_cards(self) -> None:
        self.progress.items = [
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
                    still_status="ready",
                    trakt_details_status="ready",
                    trakt_details_refreshed_at=datetime.now(tz=UTC) - timedelta(minutes=10),
                    imdb_status="ready",
                ),
                poster_status="ready",
                title_ratings_status="ready",
                title_ratings_refreshed_at=datetime.now(tz=UTC) - timedelta(minutes=10),
            )
        ]
        response = self.client.post(
            "/progress/refresh",
            json={
                "hide_upcoming": "0",
                "show_dropped": "0",
                "min_year": "",
                "use_year_filter": "0",
                "viewport_card_keys": ["progress:1"],
                "nearby_card_keys": [],
                "page_card_keys": ["progress:1"],
                "queue_after_revision": 0,
                "force_visible_refresh": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        page_tasks = self.queue.submissions[0]["page"]
        ratings_only = [task for task in page_tasks if task.payload["refresh_requests"][0]["trigger"] == "visible_ratings_refresh"]
        self.assertEqual(
            sorted(tuple(task.payload["refresh_requests"][0]["requested_parts"]) for task in ratings_only),
            [("episode_ratings",), ("title_ratings",)],
        )

    def test_progress_refresh_returns_sections_html_when_page_changes(self) -> None:
        self.unseen_episode_ids = {99}
        self.progress.items = [
            ProgressSnapshot(
                trakt_id=3,
                title="The Boys",
                completed=32,
                aired=34,
                percent_completed=94.0,
                next_episode=EpisodeSummary(
                    trakt_id=99,
                    season=5,
                    number=1,
                    title="Fifteen Inches of Sheer Dynamite",
                    still_status="checked_no_data",
                    trakt_details_status="checked_no_data",
                    imdb_status="checked_no_data",
                ),
                poster_status="checked_no_data",
                title_ratings_status="checked_no_data",
            ),
        ]
        response = self.client.post(
            "/progress/refresh",
            json={
                "hide_upcoming": "0",
                "show_dropped": "0",
                "min_year": "",
                "use_year_filter": "0",
                "viewport_card_keys": [],
                "nearby_card_keys": [],
                "page_card_keys": ["progress:1", "progress:2"],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["page_changed"])
        self.assertIn("The Boys", payload["sections_html"])
        self.assertIn("data-progress-card-key=\"progress:3\"", payload["sections_html"])
        self.assertIn("progress-new-badge", payload["sections_html"])
        self.assertIn('action="/progress/3/seen"', payload["sections_html"])
        self.assertIn("progress-seen-form", payload["sections_html"])
        self.assertIn("eye.svg", payload["sections_html"])
        self.assertNotIn("icon-button progress-seen-button", payload["sections_html"])
        self.assertNotIn("js-new-card", payload["sections_html"])
        self.assertNotIn("data-seen-form", payload["sections_html"])

    def test_progress_refresh_queues_recent_checked_no_data_still_for_visible_card(self) -> None:
        self.progress.items = [
            ProgressSnapshot(
                trakt_id=139960,
                title="The Boys",
                completed=32,
                aired=34,
                percent_completed=94.0,
                next_episode=EpisodeSummary(
                    trakt_id=12138429,
                    season=5,
                    number=1,
                    title="Fifteen Inches of Sheer Dynamite",
                    still_status="checked_no_data",
                    still_refreshed_at=datetime.now(tz=UTC) - timedelta(minutes=6),
                    first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                    trakt_details_status="ready",
                    imdb_status="ready",
                ),
                poster_status="ready",
                title_ratings_status="ready",
            ),
        ]
        response = self.client.post(
            "/progress/refresh",
            json={
                "hide_upcoming": "0",
                "show_dropped": "0",
                "min_year": "",
                "use_year_filter": "0",
                "viewport_card_keys": ["progress:139960"],
                "nearby_card_keys": [],
                "page_card_keys": ["progress:139960"],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        viewport_tasks = self.queue.submissions[0]["viewport"]
        self.assertIn("episode:139960:5:1", [task.task_key for task in viewport_tasks])
        self.assertTrue(all(task.payload["refresh_requests"][0]["trigger"] == "viewport" for task in viewport_tasks))

    def test_progress_refresh_uses_page_context_for_non_visible_buckets(self) -> None:
        response = self.client.post(
            "/progress/refresh",
            json={
                "hide_upcoming": "0",
                "show_dropped": "0",
                "min_year": "",
                "use_year_filter": "0",
                "viewport_card_keys": [],
                "nearby_card_keys": ["progress:1"],
                "page_card_keys": ["progress:1"],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(task.payload["refresh_requests"][0]["trigger"] == TRIGGER_PAGE_CONTEXT for task in self.queue.submissions[0]["nearby"]))
        self.assertTrue(all(task.payload["refresh_requests"][0]["trigger"] == TRIGGER_PAGE_CONTEXT for task in self.queue.submissions[0]["page"]))


if __name__ == "__main__":
    unittest.main()
