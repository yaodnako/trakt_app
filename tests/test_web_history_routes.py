from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trakt_tracker.application.enrich_queue import TASK_STATUS_COMPLETED
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.web.routes_history import register_history_routes


class _FakeHistoryService:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.episode_missing = False

    def history(self, *, title_type=None, title_filter=None, limit=None, offset=0):
        rows = list(self.rows)
        if title_type:
            rows = [row for row in rows if row.get("type") == title_type]
        if title_filter:
            needle = str(title_filter).casefold()
            rows = [row for row in rows if needle in str(row.get("title", "")).casefold()]
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def has_missing_visible_episode_details(self, _rows):
        return self.episode_missing

    def select_episode_enrich_keys(self, rows):
        result = []
        for row in rows:
            if row.get("type") == "show" and row.get("season") is not None and row.get("episode") is not None:
                result.append((int(row["title_trakt_id"]), int(row["season"]), int(row["episode"])))
        return result

    def enrich_visible_episode_details(self, _rows):
        return False

    def history_titles(self, title_type=None):
        rows = self.history(title_type=title_type, limit=None, offset=0)
        return sorted({str(row.get("title", "")) for row in rows if row.get("title")})


class _FakeCatalogService:
    def __init__(self) -> None:
        self.title_missing = False

    def has_missing_visible_titles(self, _rows):
        return self.title_missing

    def select_title_enrich_keys(self, rows):
        result = []
        for row in rows:
            if row.get("title_trakt_id") and row.get("type") in {"movie", "show"}:
                result.append((int(row["title_trakt_id"]), str(row["type"])))
        return result

    def enrich_visible_titles(self, _rows):
        return False


class _FakeSyncService:
    def __init__(self) -> None:
        self.changed = False

    def maybe_refresh_history(self):
        return self.changed

    def refresh_history(self):
        return None


class _FakeEnrichQueueService:
    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.updates = [
            {
                "revision": 1,
                "task_key": "title:show:1",
                "kind": "history_title",
                "status": TASK_STATUS_COMPLETED,
                "result": "ready",
                "affected_title_keys": ["03.04.2026:show:1"],
            }
        ]
        self.revision = 1
        self.running = False

    def submit_history_refresh(self, *, viewport_tasks, nearby_tasks, page_tasks):
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
            and (not keys or any(title_key in keys for title_key in update["affected_title_keys"]))
        ]
        return {
            "revision": self.revision,
            "running": self.running,
            "updates": updates,
        }

    def is_running(self, relevant_title_keys=None):
        return self.running


class _FakeBackgroundTaskManager:
    def __init__(self) -> None:
        self.running: set[str] = set()
        self.started_keys: list[str] = []

    def start(self, key: str, *, source: str, operations, fn) -> bool:
        self.running.add(key)
        self.started_keys.append(key)
        operations.publish(source, f"{source}: started.")
        return True

    def is_running(self, key: str) -> bool:
        return key in self.running

    def has_running_prefix(self, *prefixes: str) -> bool:
        return any(any(item.startswith(prefix) for prefix in prefixes) for item in self.running)


class HistoryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/templates")
        static_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/static")
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["rating_with_votes"] = lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a"
        self.templates.env.filters["episode_label"] = lambda season, episode: f"S{int(season):02d}E{int(episode):02d}" if season is not None and episode is not None else ""
        self.templates.env.filters["cached_image_url"] = lambda value: value or ""
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.history = _FakeHistoryService(
            [
                self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC)),
            ]
        )
        self.catalog = _FakeCatalogService()
        self.sync = _FakeSyncService()
        self.enrich_queue = _FakeEnrichQueueService()
        self.operations = OperationLog()
        self.app.state.services = SimpleNamespace(
            history=self.history,
            catalog=self.catalog,
            enrich_queue=self.enrich_queue,
            sync=self.sync,
            operations=self.operations,
            auth=SimpleNamespace(
                config=SimpleNamespace(utc_offset="+03:00"),
                is_authorized=lambda: True,
                is_configured=lambda: True,
            ),
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
                "debug_initial_seq": self.operations.current_seq(),
            }
            base_context.update(context)
            return self.templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

        def render_fragment(request: Request, template_name: str, context: dict) -> str:
            fragment_context = {"request": request, "current_path": request.url.path}
            fragment_context.update(context)
            return self.templates.get_template(template_name).render(fragment_context)

        register_history_routes(self.app, render=render, render_fragment=render_fragment)
        self.client = TestClient(self.app)

    def test_history_refresh_returns_only_requested_visible_title_keys(self) -> None:
        show_key = "03.04.2026:show:1"
        movie_key = "03.04.2026:movie:2"
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [show_key],
                "nearby_title_keys": [],
                "page_title_keys": [show_key, movie_key],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["title_key"] for item in payload["title_groups"]], [show_key])
        self.assertEqual(payload["missing_title_keys"], [])
        self.assertEqual(len(self.enrich_queue.submissions), 1)
        self.assertEqual([task.task_key for task in self.enrich_queue.submissions[0]["viewport"]], ["title:show:1", "episode:1:1:1"])

    def test_history_refresh_marks_missing_title_keys_when_card_disappears(self) -> None:
        self.history.rows = [self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC))]
        show_key = "03.04.2026:show:1"
        movie_key = "03.04.2026:movie:2"
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [show_key],
                "nearby_title_keys": [],
                "page_title_keys": [show_key, movie_key],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["missing_title_keys"], [movie_key])
        self.assertTrue(payload["page_changed"])

    def test_history_refresh_reports_page_changed_when_order_differs(self) -> None:
        show_key = "03.04.2026:show:1"
        movie_key = "03.04.2026:movie:2"
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [show_key],
                "nearby_title_keys": [movie_key],
                "page_title_keys": [show_key, movie_key],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["page_changed"])
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [movie_key],
                "nearby_title_keys": [show_key],
                "page_title_keys": [movie_key, show_key],
                "queue_after_revision": 0,
            },
        )
        self.assertTrue(response.json()["page_changed"])

    def test_history_page_uses_no_reload_refresh_script(self) -> None:
        response = self.client.get("/history?page=2")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-history-page="2"', html)
        self.assertIn("/history/refresh", html)
        self.assertNotIn("window.location.reload()", html)
        self.assertNotIn("history_enrich_initial_seq", html)
        self.assertNotIn("reloadGuardKey", html)
        self.assertEqual(self.app.state.bg_tasks.started_keys, [])

    def test_history_uses_distinct_card_keys_for_same_title_on_different_days(self) -> None:
        self.history.rows = [
            self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
            self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 2, 12, 0, tzinfo=UTC)),
        ]
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-history-title-key="03.04.2026:show:1"', html)
        self.assertIn('data-history-title-key="02.04.2026:show:1"', html)

    def test_history_template_renders_loading_states_for_unknown_statuses(self) -> None:
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("poster-loading", html)
        self.assertIn("history-entry-still-loading", html)
        self.assertIn("Loading", html)

    def test_history_template_renders_terminal_empty_states_for_checked_no_data(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                "title_poster_status": "checked_no_data",
                "poster_url": "",
                "title_ratings_status": "checked_no_data",
                "title_trakt_rating": None,
                "title_imdb_rating": None,
                "episode_still_status": "checked_no_data",
                "episode_trakt_status": "checked_no_data",
                "episode_imdb_status": "checked_no_data",
            }
        ]
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("No poster", html)
        self.assertIn("No preview", html)
        self.assertGreaterEqual(html.count("n/a"), 4)

    @staticmethod
    def _row(title_type: str, trakt_id: int, title: str, *, watched_at: datetime) -> dict:
        return {
            "title_trakt_id": trakt_id,
            "title": title,
            "title_slug": title.lower(),
            "poster_url": "",
            "title_poster_status": "unknown",
            "title_trakt_rating": None,
            "title_trakt_votes": None,
            "title_imdb_rating": None,
            "title_imdb_votes": None,
            "title_ratings_status": "unknown",
            "title_episode_avg_rating": None,
            "title_episode_rated_count": 0,
            "type": title_type,
            "action": "watched",
            "watched_at": watched_at,
            "season": 1 if title_type == "show" else None,
            "episode": 1 if title_type == "show" else None,
            "episode_title": "Episode 1" if title_type == "show" else None,
            "episode_still_url": "",
            "episode_still_status": "unknown",
            "episode_trakt_rating": None,
            "episode_trakt_votes": None,
            "episode_trakt_status": "unknown",
            "episode_imdb_rating": None,
            "episode_imdb_votes": None,
            "episode_imdb_status": "unknown",
            "event_rating": None,
            "title_rating": None,
            "display_rating": None,
        }


if __name__ == "__main__":
    unittest.main()
