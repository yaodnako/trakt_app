from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trakt_tracker.application.episode_ratings_matrix import rating_bucket_color
from trakt_tracker.application.enrich_queue import TASK_STATUS_COMPLETED
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_STILL,
    TRIGGER_PAGE_CONTEXT,
    metadata_refresh_due,
)
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.routes_history import register_history_routes
from trakt_tracker.web.routes_ratings import register_rating_routes
from trakt_tracker.web.viewmodels import HISTORY_PAGE_SIZE

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "trakt_tracker" / "web" / "static"


class _FakeHistoryService:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.episode_missing = False
        self.ratings: list[tuple[RatingInput, str]] = []
        self.history_requests: list[dict] = []
        self.title_summary_requests: list[dict] = []

    def history(self, *, title_type=None, title_filter=None, rated_only=False, limit=None, offset=0):
        self.history_requests.append({"limit": limit, "offset": offset})
        rows = list(self.rows)
        if title_type:
            rows = [row for row in rows if row.get("type") == title_type]
        if title_filter:
            needle = str(title_filter).casefold()
            rows = [row for row in rows if needle in str(row.get("title", "")).casefold()]
        if rated_only:
            rows = [
                row
                for row in rows
                if row.get("type") == "show"
                and row.get("season") is not None
                and row.get("episode") is not None
                and row.get("display_rating") is not None
            ]
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def history_title_summaries(
        self,
        *,
        title_type=None,
        title_filter=None,
        rated_only=False,
        sort_by="last_watched",
        sort_direction="desc",
        limit=None,
        offset=0,
    ):
        self.title_summary_requests.append({"limit": limit, "offset": offset})
        rows = self.history(title_type=title_type, title_filter=title_filter, rated_only=False, limit=None, offset=0)
        groups = {}
        for row in rows:
            key = (row.get("type"), row.get("title_trakt_id"))
            group = groups.get(key)
            if group is None:
                group = {
                    "title_key": f"{row.get('type')}:{row.get('title_trakt_id')}",
                    "provider": row.get("provider", "trakt"),
                    "tmdb_id": row.get("tmdb_id"),
                    "title_trakt_id": row.get("title_trakt_id"),
                    "title": row.get("title", ""),
                    "title_slug": row.get("title_slug", ""),
                    "poster_url": row.get("poster_url", ""),
                    "title_poster_status": row.get("title_poster_status", "unknown"),
                    "title_trakt_rating": row.get("title_trakt_rating"),
                    "title_trakt_votes": row.get("title_trakt_votes"),
                    "title_tmdb_rating": row.get("title_tmdb_rating"),
                    "title_tmdb_votes": row.get("title_tmdb_votes"),
                    "title_imdb_rating": row.get("title_imdb_rating"),
                    "title_imdb_votes": row.get("title_imdb_votes"),
                    "title_ratings_status": row.get("title_ratings_status", "unknown"),
                    "title_ratings_refreshed_at": row.get("title_ratings_refreshed_at"),
                    "title_episode_avg_rating": row.get("title_episode_avg_rating"),
                    "title_episode_rated_count": row.get("title_episode_rated_count", 0),
                    "my_rating": row.get("title_episode_avg_rating") if row.get("type") == "show" else row.get("display_rating"),
                    "title_rating": row.get("title_rating"),
                    "type": row.get("type", ""),
                    "title_year": row.get("title_year"),
                    "last_watched_at": row.get("watched_at"),
                    "last_watched_at_known": row.get("watched_at_known", True),
                    "watched_count": 0,
                    "latest_season": row.get("season"),
                    "latest_episode": row.get("episode"),
                }
                groups[key] = group
            group["watched_count"] += 1
        result = [group for group in groups.values() if not rated_only or group.get("my_rating") is not None]
        normalized_sort = sort_by if sort_by in {"rating", "last_watched", "release_year"} else "last_watched"

        def sort_value(group):
            if normalized_sort == "rating":
                return group.get("my_rating")
            if normalized_sort == "release_year":
                return group.get("title_year")
            if not group.get("last_watched_at_known", True):
                return None
            watched_at = group.get("last_watched_at")
            return watched_at.timestamp() if isinstance(watched_at, datetime) else None

        def tie_key(group):
            return (str(group.get("title", "")).casefold(), int(group.get("title_trakt_id") or 0))

        known = [(sort_value(group), group) for group in result if sort_value(group) is not None]
        unknown = [group for group in result if sort_value(group) is None]
        known.sort(key=lambda item: tie_key(item[1]))
        known.sort(key=lambda item: item[0], reverse=sort_direction != "asc")
        unknown.sort(key=tie_key)
        result = [group for _value, group in known] + unknown
        if offset:
            result = result[offset:]
        if limit is not None:
            result = result[:limit]
        return result

    def has_missing_visible_episode_details(self, _rows):
        return self.episode_missing

    def select_episode_enrich_keys(self, rows, *, trigger="viewport", requested_parts=(), refresh_requests=None):
        result = []
        for row in rows:
            if row.get("type") != "show" or row.get("season") is None or row.get("episode") is None:
                continue
            wants_still = not requested_parts or "still" in requested_parts
            wants_ratings = not requested_parts or "episode_ratings" in requested_parts
            still_due = wants_still and metadata_refresh_due(
                ASSET_KIND_STILL,
                status=row.get("episode_still_status"),
                last_checked_at=row.get("episode_still_refreshed_at"),
                has_value=bool(row.get("episode_still_url")),
                trigger=trigger,
                first_aired=row.get("episode_first_aired"),
            ).should_refresh
            ratings_due = wants_ratings and (
                row.get("episode_trakt_status") in {"unknown", "retryable_failure"}
                or (requested_parts and "episode_ratings" in requested_parts and row.get("episode_trakt_status") == "ready")
                or row.get("episode_imdb_status") in {"unknown", "retryable_failure"}
            )
            if still_due or ratings_due:
                result.append((int(row["title_trakt_id"]), int(row["season"]), int(row["episode"])))
        return result

    def enrich_visible_episode_details(self, _rows):
        return False

    def history_titles(self, title_type=None):
        rows = self.history(title_type=title_type, limit=None, offset=0)
        return sorted({str(row.get("title", "")) for row in rows if row.get("title")})

    def set_rating(self, item: RatingInput, title: str = "") -> None:
        self.ratings.append((item, title))


class _FakeCatalogService:
    def __init__(self) -> None:
        self.title_missing = False

    def has_missing_visible_titles(self, _rows):
        return self.title_missing

    def select_title_enrich_keys(self, rows, *, trigger="viewport", requested_parts=(), refresh_requests=None):
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

    def sync_assets_full(self):
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


class _FakeInteractionsService:
    def __init__(self) -> None:
        self.ratings: list[tuple[RatingInput, str]] = []

    def save_rating(self, item: RatingInput, *, title: str = "") -> None:
        self.ratings.append((item, title))


class HistoryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        static_dir = STATIC_DIR
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["rating_with_votes"] = lambda rating, votes: f"{rating} ({votes})" if rating is not None else "n/a"
        self.templates.env.filters["rating_bucket_color"] = rating_bucket_color
        self.templates.env.filters["episode_label"] = lambda season, episode, imdb_season=None, imdb_episode=None: (
            f"S{int(season):02d}E{int(episode):02d}"
            + (
                f" (S{int(imdb_season):02d}E{int(imdb_episode):02d})"
                if imdb_season is not None
                and imdb_episode is not None
                and (int(imdb_season), int(imdb_episode)) != (int(season), int(episode))
                else ""
            )
            if season is not None and episode is not None
            else ""
        )
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
        self.interactions = _FakeInteractionsService()
        self.app.state.services = SimpleNamespace(
            history=self.history,
            catalog=self.catalog,
            enrich_queue=self.enrich_queue,
            sync=self.sync,
            operations=self.operations,
            interactions=self.interactions,
            auth=SimpleNamespace(
                config=SimpleNamespace(utc_offset="+03:00", catalog_provider_mode="trakt"),
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
                "catalog_provider_mode": self.app.state.services.auth.config.catalog_provider_mode,
                "notification_sound_url": "",
                "debug_mode": False,
                "debug_initial_seq": self.operations.current_seq(),
            }
            base_context.update(context)
            return self.templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

        def render_fragment(request: Request, template_name: str, context: dict) -> str:
            fragment_context = {
                "request": request,
                "current_path": request.url.path,
                "catalog_provider_mode": self.app.state.services.auth.config.catalog_provider_mode,
            }
            fragment_context.update(context)
            return self.templates.get_template(template_name).render(fragment_context)

        register_rating_routes(self.app)
        register_history_routes(self.app, render=render, render_fragment=render_fragment)
        self.client = TestClient(self.app)

    def test_tmdb_mode_history_auto_sync_does_not_start_trakt_work(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        self.app.state.services.tmdb_catalog = SimpleNamespace(
            local_history_rows=lambda **_kwargs: [],
            local_history_title_summaries=lambda **_kwargs: [],
        )

        response = self.client.get("/history/auto-sync")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["started"])
        self.assertEqual(self.app.state.bg_tasks.started_keys, [])

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

    def test_history_refresh_queues_ratings_only_requests_for_stale_visible_items(self) -> None:
        stale_row = self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC))
        stale_row["title_ratings_status"] = "ready"
        stale_row["title_ratings_refreshed_at"] = datetime.now(tz=UTC) - timedelta(minutes=10)
        stale_row["episode_trakt_status"] = "ready"
        stale_row["episode_trakt_refreshed_at"] = datetime.now(tz=UTC) - timedelta(minutes=10)
        self.history.rows = [stale_row]
        show_key = "03.04.2026:show:1"
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [show_key],
                "nearby_title_keys": [],
                "page_title_keys": [show_key],
                "queue_after_revision": 0,
                "force_visible_refresh": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        page_tasks = self.enrich_queue.submissions[0]["page"]
        ratings_only = [task for task in page_tasks if task.payload["refresh_requests"][0]["trigger"] == "visible_ratings_refresh"]
        self.assertEqual(
            sorted(tuple(task.payload["refresh_requests"][0]["requested_parts"]) for task in ratings_only),
            [("episode_ratings",), ("title_ratings",)],
        )

    def test_history_page_uses_no_reload_refresh_script(self) -> None:
        response = self.client.get("/history?page=2")
        self.assertEqual(response.status_code, 200)
        html = response.text
        history_script = (STATIC_DIR / "history_page.js").read_text(encoding="utf-8")
        self.assertIn('data-history-page="2"', html)
        self.assertIn("history_page.js?v=", html)
        self.assertIn("/history/refresh", history_script)
        self.assertIn('historyTypeSelect?.addEventListener("change", applyImmediately)', history_script)
        self.assertIn('titleFilterInput.addEventListener("input"', history_script)
        self.assertNotIn(">Apply</button>", html)
        self.assertNotIn('action="/history/sync"', html)
        self.assertNotIn(">Sync</button>", html)
        self.assertNotIn("window.location.reload()", history_script)
        self.assertNotIn("history_enrich_initial_seq", history_script)
        self.assertNotIn('id="history-sync-status"', html)
        self.assertIn("showSyncToast", history_script)
        self.assertIn("window.traktDebugMode && running", history_script)
        self.assertIn('window.addEventListener("wheel", this.onScrollActivity, {passive: true})', history_script)
        self.assertIn("await this.waitForScrollIdle()", history_script)
        self.assertIn("await this.yieldToBrowser()", history_script)
        self.assertIn("if (domChanged)", history_script)
        self.assertIn("sort: this.historySort", history_script)
        self.assertIn("sort_dir: this.historySortDirection", history_script)
        self.assertNotIn("reloadGuardKey", history_script)
        self.assertEqual(self.app.state.bg_tasks.started_keys, [])

    def test_history_pager_scrolls_new_page_to_list_start(self) -> None:
        history_script = (STATIC_DIR / "history_page.js").read_text(encoding="utf-8")

        self.assertIn("scrollToPageStart = false", history_script)
        self.assertIn(
            'const scrollToPageStart = Boolean(link.closest(".history-pager"));',
            history_script,
        )
        self.assertIn(
            "navigateHistory(new URL(link.href, window.location.href), {scrollToPageStart});",
            history_script,
        )
        self.assertIn(
            'pageRoot?.scrollIntoView({block: "start", behavior: "auto"});',
            history_script,
        )

    def test_history_rate_query_accepts_empty_season_episode(self) -> None:
        response = self.client.get(
            "/history?rate_trakt_id=2&rate_type=movie&rate_season=&rate_episode=&rate_title=Dune"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("data-rating-autopen", response.text)

    def test_tmdb_mode_history_includes_local_episode_and_rating(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        watched_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        local_row = {
            "provider": "tmdb",
            "tmdb_id": 43125,
            "title_trakt_id": 0,
            "title": "Guilty Crown",
            "title_slug": "",
            "poster_url": "https://poster.example/guilty-crown.jpg",
            "title_poster_status": "ready",
            "backdrop_url": "",
            "title_backdrop_status": "checked_no_data",
            "title_tmdb_rating": 7.4,
            "title_tmdb_votes": 2300,
            "title_trakt_rating": None,
            "title_trakt_votes": None,
            "title_imdb_rating": 7.0,
            "title_imdb_votes": 18100,
            "title_ratings_status": "ready",
            "title_episode_avg_rating": 9.0,
            "title_episode_rated_count": 1,
            "type": "show",
            "action": "watched",
            "watched_at": watched_at,
            "watched_at_known": True,
            "season": 1,
            "episode": 1,
            "episode_title": "Genesis",
            "episode_still_url": "",
            "episode_still_status": "checked_no_data",
            "episode_tmdb_rating": 7.8,
            "episode_tmdb_votes": 120,
            "episode_trakt_rating": None,
            "episode_trakt_votes": None,
            "episode_trakt_status": "checked_no_data",
            "episode_imdb_rating": 7.6,
            "episode_imdb_votes": 200,
            "episode_imdb_status": "ready",
            "display_rating": 9,
        }
        local_title = {
            **local_row,
            "title_key": "show:tmdb:43125",
            "my_rating": 9.0,
            "last_watched_at": watched_at,
            "last_watched_at_known": True,
            "watched_count": 1,
            "latest_season": 1,
            "latest_episode": 1,
        }
        self.app.state.services.tmdb_catalog = SimpleNamespace(
            local_history_rows=lambda **_kwargs: [local_row],
            local_history_title_summaries=lambda **_kwargs: [local_title],
            local_history_titles=lambda **_kwargs: ["Guilty Crown"],
        )

        episodes = self.client.get("/history")
        titles = self.client.get("/history?view=titles")

        self.assertEqual(episodes.status_code, 200)
        self.assertIn("Guilty Crown", episodes.text)
        self.assertIn("Genesis", episodes.text)
        self.assertIn('data-rating-provider="tmdb"', episodes.text)
        self.assertIn('data-rating-tmdb-id="43125"', episodes.text)
        self.assertIn("data-tmdb-history-unwatch", episodes.text)
        self.assertEqual(titles.status_code, 200)
        self.assertIn('data-history-title-key="show:tmdb:43125"', titles.text)

    def test_tmdb_history_reads_only_local_projection_page_window(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        local_row_requests: list[dict] = []
        local_title_requests: list[dict] = []
        self.app.state.services.tmdb_catalog = SimpleNamespace(
            local_history_rows=lambda **kwargs: local_row_requests.append(kwargs) or [],
            local_history_title_summaries=lambda **kwargs: local_title_requests.append(kwargs) or [],
            local_history_titles=lambda **_kwargs: [],
        )
        expected_limit = HISTORY_PAGE_SIZE + 1

        episodes = self.client.get("/history?page=2")

        self.assertEqual(episodes.status_code, 200)
        self.assertEqual(self.history.history_requests, [])
        self.assertEqual(local_row_requests[-1]["limit"], expected_limit)
        self.assertEqual(local_row_requests[-1]["offset"], HISTORY_PAGE_SIZE)

        titles = self.client.get("/history?view=titles&page=2")

        self.assertEqual(titles.status_code, 200)
        self.assertEqual(self.history.title_summary_requests, [])
        self.assertEqual(local_title_requests[-1]["limit"], expected_limit)
        self.assertEqual(local_title_requests[-1]["offset"], HISTORY_PAGE_SIZE)

    def test_history_unrated_card_uses_rating_modal_trigger(self) -> None:
        self.history.rows = [self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC))]
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("data-rating-trigger", html)
        self.assertIn('data-rating-title-type="movie"', html)
        self.assertNotIn("rate_season=", html)

    def test_ratings_endpoint_saves_movie_rating(self) -> None:
        response = self.client.post(
            "/ratings",
            json={
                "title_type": "movie",
                "trakt_id": 2,
                "title": "Dune",
                "rating": 9,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        item, title = self.history.ratings[-1]
        self.assertEqual(item.title_type, "movie")
        self.assertEqual(item.trakt_id, 2)
        self.assertEqual(item.rating, 9)
        self.assertIsNone(item.season)
        self.assertIsNone(item.episode)
        self.assertEqual(title, "Dune")

    def test_ratings_endpoint_saves_show_episode_rating(self) -> None:
        response = self.client.post(
            "/ratings",
            json={
                "title_type": "show",
                "trakt_id": 1,
                "title": "Severance",
                "season": "1",
                "episode": "2",
                "rating": 8,
            },
        )
        self.assertEqual(response.status_code, 200)
        item, title = self.history.ratings[-1]
        self.assertEqual(item.title_type, "show")
        self.assertEqual(item.trakt_id, 1)
        self.assertEqual(item.rating, 8)
        self.assertEqual(item.season, 1)
        self.assertEqual(item.episode, 2)
        self.assertEqual(title, "Severance")

    def test_ratings_endpoint_saves_unmapped_tmdb_episode_rating_locally(self) -> None:
        calls: list[dict] = []
        tmdb_item = SimpleNamespace(
            title_type="show",
            tmdb_id=43125,
            trakt_id=None,
            title="Guilty Crown",
        )
        self.app.state.services.tmdb_catalog = SimpleNamespace(
            get_item=lambda _title_type, _tmdb_id: tmdb_item,
            set_rating=lambda _item, **kwargs: calls.append(kwargs) or {
                "rating": kwargs["rating"],
                "local_only": True,
                "trakt_id": None,
            },
        )

        response = self.client.post(
            "/ratings",
            json={
                "provider": "tmdb",
                "title_type": "show",
                "tmdb_id": 43125,
                "title": "Guilty Crown",
                "season": 1,
                "episode": 2,
                "rating": 9,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["local_only"])
        self.assertEqual(
            calls,
            [{"rating": 9, "season": 1, "episode": 2}],
        )

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

    def test_history_orders_entries_inside_title_card_oldest_to_newest(self) -> None:
        newer = {
            **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
            "episode": 2,
            "episode_title": "Episode 2",
        }
        older = self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC))
        self.history.rows = [newer, older]

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertLess(html.index("Episode 1"), html.index("Episode 2"))

    def test_history_groups_undated_rows_at_without_date_label(self) -> None:
        self.history.rows = [
            self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC)),
            {
                **self._row("movie", 4, "Arrival", watched_at=datetime(1970, 1, 1, tzinfo=UTC)),
                "watched_at_known": False,
            },
        ]
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Без даты", html)
        self.assertIn('data-history-title-key="Без даты:movie:4"', html)
        self.assertLess(html.index("03.04.2026"), html.index("Без даты"))

    def test_history_template_renders_loading_states_for_unknown_statuses(self) -> None:
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("poster-loading", html)
        self.assertIn("history-entry-still-loading", html)
        self.assertIn('data-title-matrix-url="/titles/show/1/episode-ratings-matrix"', html)
        self.assertIn("Loading", html)

    def test_history_title_links_to_trakt_and_show_poster_opens_watch_panel(self) -> None:
        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('<a href="https://trakt.tv/shows/severance" target="_blank" rel="noreferrer">Severance</a>', html)
        self.assertIn('data-watch-panel-url="/search/show/1/watch-panel"', html)
        self.assertIn('data-trakt-id="1"', html)
        self.assertIn('href="https://trakt.tv/movies/dune"', html)
        self.assertIn('id="history-watch-overlay"', html)
        self.assertIn("data-show-watch-play", html)
        history_watch_script = (STATIC_DIR / "history_watch_panel.js").read_text(encoding="utf-8")
        ui_script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")
        self.assertIn("configurePlayAction(watchOverlay, trigger)", history_watch_script)
        self.assertIn("show_watch_panel.js?v=", html)
        self.assertIn('data-history-watch-date-mode="now"', html)
        self.assertNotIn("scheduleWatchPanelRefreshIfPending", html)
        self.assertIn("refreshWatchPanel", history_watch_script)
        self.assertIn("data-search-unwatch-action", html)
        self.assertIn('title="Remove movie from watched history"', html)
        self.assertIn('data-title-type="movie"', html)
        self.assertIn('class="history-episode-footer"', html)
        self.assertIn("seen.svg", html)
        self.assertIn("cancel.svg", html)
        panel_script = (STATIC_DIR / "show_watch_panel.js").read_text(encoding="utf-8")
        panel_css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        self.assertIn("patchArtwork", panel_script)
        self.assertIn("findRatingTrigger", ui_script)
        self.assertIn("search-watch-overlay:not([hidden])", ui_script)
        self.assertIn("const managedInertNodes = new Set()", ui_script)
        self.assertIn("activeBranch.parentElement.children", ui_script)
        self.assertNotIn('document.querySelector(".shell").inert', ui_script)
        self.assertNotIn("scrollIntoView", panel_script)
        self.assertNotIn("window.confirm", panel_script)
        self.assertIn("window.traktConfirm", panel_script)
        self.assertIn("const scrollTop = body.scrollTop", panel_script)
        self.assertIn("body.scrollTop = scrollTop", panel_script)
        self.assertIn("overflow-anchor: none", panel_css)
        self.assertIn(".history-episode-footer", panel_css)

        title_mode = self.client.get("/history?view=titles&page=1")
        self.assertIn('title="Remove movie from watched history"', title_mode.text)
        self.assertEqual(title_mode.status_code, 200)
        self.assertIn('data-watch-panel-url="/search/show/1/watch-panel"', title_mode.text)
        self.assertIn('<a href="https://trakt.tv/shows/severance" target="_blank" rel="noreferrer">Severance</a>', title_mode.text)

    def test_history_page_title_mode_renders_compact_title_cards(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 4, 12, 0, tzinfo=UTC)),
                "title_episode_avg_rating": 8.5,
                "title_episode_rated_count": 2,
            },
            self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
            {
                **self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 2, 11, 0, tzinfo=UTC)),
                "display_rating": 9,
            },
        ]
        response = self.client.get("/history?view=titles&type=all")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertNotIn("Watched titles", html)
        self.assertIn('data-history-view="titles"', html)
        self.assertIn('data-history-title-key="show:1"', html)
        self.assertIn('data-history-title-key="movie:2"', html)
        self.assertNotIn('data-history-title-key="04.04.2026:show:1"', html)
        self.assertNotIn("history-episode-card", html)
        self.assertIn('data-title-matrix-url="/titles/show/1/episode-ratings-matrix"', html)
        self.assertIn('class="history-rating-badge user-rating-badge poster-average-rating-badge"', html)
        self.assertIn('style="--user-rating-color: rgb(40, 180, 99);"', html)
        self.assertIn('<span class="user-rating-value">8.5</span>', html)
        self.assertIn('<span class="user-rating-star">&#9733;</span>', html)
        self.assertIn('class="history-rating-badge user-rating-badge"', html)
        self.assertIn('style="--user-rating-color: rgb(24, 106, 59);"', html)
        self.assertIn('<span class="user-rating-value">9</span>', html)
        self.assertIn('href="https://trakt.tv/shows/severance"', html)

    def test_history_grouped_show_average_rating_uses_imdb_palette(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 4, 12, 0, tzinfo=UTC)),
                "title_episode_avg_rating": 6.5,
                "title_episode_rated_count": 2,
            },
        ]

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="history-rating-badge user-rating-badge poster-average-rating-badge"', response.text)
        self.assertIn('style="--user-rating-color: rgb(243, 156, 18);"', response.text)
        self.assertIn('<span class="user-rating-value">6.5</span>', response.text)
        self.assertIn('<span class="user-rating-star">&#9733;</span>', response.text)

    def test_history_episode_and_movie_ratings_use_shared_badge_style(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 4, 12, 0, tzinfo=UTC)),
                "display_rating": 5,
            },
            {
                **self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                "display_rating": 9,
            },
        ]

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="history-rating-badge user-rating-badge episode-user-rating-badge"', response.text)
        self.assertIn('data-user-rating="5"', response.text)
        self.assertIn('style="--user-rating-color: rgb(231, 76, 60);"', response.text)
        self.assertIn('<span class="user-rating-value">5</span>', response.text)
        self.assertIn('<span class="user-rating-star">&#9733;</span>', response.text)
        self.assertIn('class="history-rating-badge user-rating-badge"', response.text)
        self.assertIn('data-user-rating="9"', response.text)
        self.assertIn('style="--user-rating-color: rgb(24, 106, 59);"', response.text)

    def test_history_title_mode_sorts_in_both_directions_with_nulls_last(self) -> None:
        self.history.rows = [
            {
                **self._row("movie", 1, "Alpha", watched_at=datetime(2026, 4, 1, tzinfo=UTC)),
                "display_rating": 8,
                "title_year": 2020,
            },
            {
                **self._row("movie", 2, "Beta", watched_at=datetime(1970, 1, 1, tzinfo=UTC)),
                "watched_at_known": False,
            },
            {
                **self._row("movie", 3, "Gamma", watched_at=datetime(2026, 4, 2, tzinfo=UTC)),
                "display_rating": 6,
                "title_year": 2024,
            },
        ]
        scenarios = [
            ("rating", "asc", [3, 1, 2]),
            ("rating", "desc", [1, 3, 2]),
            ("last_watched", "asc", [1, 3, 2]),
            ("last_watched", "desc", [3, 1, 2]),
            ("release_year", "asc", [1, 3, 2]),
            ("release_year", "desc", [3, 1, 2]),
        ]

        for sort_by, sort_direction, expected_ids in scenarios:
            with self.subTest(sort_by=sort_by, sort_direction=sort_direction):
                response = self.client.get(
                    f"/history?view=titles&sort={sort_by}&sort_dir={sort_direction}"
                )
                self.assertEqual(response.status_code, 200)
                html = response.text
                positions = [html.index(f'data-history-title-key="movie:{trakt_id}"') for trakt_id in expected_ids]
                self.assertEqual(positions, sorted(positions))

    def test_history_title_sort_state_is_normalized_and_preserved(self) -> None:
        response = self.client.get("/history?view=titles&sort=invalid&sort_dir=sideways")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('data-history-sort="last_watched"', html)
        self.assertIn('data-history-sort-direction="desc"', html)
        self.assertIn('<option value="last_watched" selected>Last watched</option>', html)
        self.assertIn('aria-label="Sort descending; activate to reverse"', html)
        self.assertIn("&sort=last_watched&sort_dir=desc", html)

        episode_response = self.client.get("/history?view=episodes&sort=rating&sort_dir=asc")
        self.assertEqual(episode_response.status_code, 200)
        episode_html = episode_response.text
        self.assertNotIn('aria-label="Sort watched titles"', episode_html)
        self.assertIn('name="sort" value="rating"', episode_html)
        self.assertIn("&sort=rating&sort_dir=asc", episode_html)

    def test_history_title_sort_state_is_preserved_by_pager(self) -> None:
        self.history.rows = [
            {
                **self._row(
                    "movie",
                    trakt_id,
                    f"Movie {trakt_id}",
                    watched_at=datetime(2026, 4, 1, tzinfo=UTC),
                ),
                "display_rating": 8,
            }
            for trakt_id in range(1, 52)
        ]

        response = self.client.get("/history?view=titles&sort=rating&sort_dir=asc")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "page=2&view=titles&sort=rating&sort_dir=asc",
            response.text,
        )

    def test_history_title_sort_state_is_preserved_by_action_redirects(self) -> None:
        sync_response = self.client.post(
            "/history/sync",
            data={
                "type": "all",
                "title_filter": "",
                "rated_only": "0",
                "view": "titles",
                "sort": "release_year",
                "sort_dir": "asc",
                "page": "2",
            },
            follow_redirects=False,
        )
        self.assertEqual(sync_response.status_code, 303)
        self.assertIn("sort=release_year&sort_dir=asc", sync_response.headers["location"])

        rate_response = self.client.post(
            "/history/rate",
            data={
                "type": "all",
                "title_filter": "",
                "rated_only": "0",
                "view": "titles",
                "sort": "rating",
                "sort_dir": "desc",
                "page": "1",
                "trakt_id": "2",
                "rating_type": "movie",
                "title_value": "Dune",
                "rating": "9",
            },
            follow_redirects=False,
        )
        self.assertEqual(rate_response.status_code, 303)
        self.assertIn("sort=rating&sort_dir=desc", rate_response.headers["location"])

    def test_history_movie_cards_do_not_render_matrix_trigger(self) -> None:
        self.history.rows = [self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC))]
        response = self.client.get("/history?page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertNotIn('data-title-matrix-url="/titles/show/', html)

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

    def test_history_images_use_proxy_retry_without_direct_cdn_fallback(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                "poster_url": "https://poster.example/severance.jpg",
                "episode_still_url": "https://still.example/severance.jpg",
            }
        ]

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        html = response.text
        ui_script = (STATIC_DIR / "ui_core.js").read_text(encoding="utf-8")
        self.assertIn('current.pathname !== "/cached-image"', ui_script)
        self.assertNotIn("data-direct-src", html)
        self.assertNotIn("dataset.directSrc", html)

    def test_history_episode_label_appends_different_imdb_coordinates(self) -> None:
        self.history.rows[0]["episode_imdb_season"] = 2
        self.history.rows[0]["episode_imdb_episode"] = 1

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("S01E01 (S02E01)", response.text)

    def test_tmdb_history_uses_only_imdb_coordinate_for_mapped_legacy_row(self) -> None:
        self.app.state.services.auth.config.catalog_provider_mode = "tmdb_preview"
        self.history.rows[0].update(
            {
                "provider": "trakt",
                "tmdb_id": 95396,
                "title_tmdb_rating": 8.4,
                "title_tmdb_votes": 772,
                "episode_imdb_season": 2,
                "episode_imdb_episode": 1,
                "episode_still_url": "https://still.example/severance.jpg",
                "episode_still_status": "ready",
                "episode_tmdb_rating": 7.2,
                "episode_tmdb_votes": 11,
                "episode_tmdb_status": "ready",
            }
        )

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        card = response.text.split('data-history-title-key="03.04.2026:show:1"', 1)[1].split("</article>", 1)[0]
        self.assertIn("S02E01", card)
        self.assertNotIn("S01E01 (S02E01)", card)
        self.assertIn("https://www.themoviedb.org/tv/95396/season/1/episode/1", card)
        self.assertIn("data-tmdb-card", card)
        self.assertIn("data-tmdb-progress-watch-panel", card)
        self.assertIn('data-title-matrix-url="/titles/tmdb/show/95396/episode-ratings-matrix"', card)
        self.assertNotIn("trakt.tv", card)
        self.assertNotIn("data-rating-trakt-id", card)
        self.assertNotIn("data-trakt-id", card)

    def test_history_movie_preview_uses_external_trakt_link(self) -> None:
        template = (PROJECT_ROOT / "trakt_tracker" / "web" / "templates" / "history_title_card.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('href="{{ trakt_episode_url or trakt_title_url }}"', template)
        self.assertNotIn("('/titles/' ~ row.type", template)

    def test_history_mobile_filter_and_accessibility_contracts(self) -> None:
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        template = (PROJECT_ROOT / "trakt_tracker" / "web" / "templates" / "history.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("flex: 0 0 auto", css)
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn('aria-label="Filter history by title"', template)
        self.assertNotIn("history-pager-top", template)
        self.assertIn("history-pager-bottom", template)
        self.assertIn("No matches", template)

    def test_history_show_episode_without_still_does_not_use_title_poster_fallback(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                "poster_url": "https://poster.example/severance.jpg",
                "episode_still_url": "",
                "episode_still_status": "unknown",
            }
        ]

        response = self.client.get("/history?page=1")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("history-entry-still-loading", html)
        self.assertIn("Loading", html)
        self.assertEqual(html.count("https://poster.example/severance.jpg"), 1)

    def test_history_title_mode_shows_na_for_ready_status_without_imdb_value(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                "title_trakt_rating": 8.4,
                "title_trakt_votes": 1234,
                "title_imdb_rating": None,
                "title_imdb_votes": None,
                "title_ratings_status": "ready",
            }
        ]
        response = self.client.get("/history?view=titles&page=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("8.4", html)
        self.assertIn("n/a", html)

    def test_history_refresh_queues_recent_checked_no_data_still_for_visible_row(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 139960, "The Boys", watched_at=datetime(2026, 4, 9, 12, 0, tzinfo=UTC)),
                "episode_still_status": "checked_no_data",
                "episode_still_refreshed_at": datetime.now(tz=UTC) - timedelta(minutes=6),
                "episode_first_aired": datetime.now(tz=UTC) - timedelta(days=1),
                "episode_trakt_status": "ready",
                "episode_imdb_status": "ready",
            }
        ]
        title_key = "09.04.2026:show:139960"
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [title_key],
                "nearby_title_keys": [],
                "page_title_keys": [title_key],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        viewport_tasks = self.enrich_queue.submissions[0]["viewport"]
        self.assertIn("episode:139960:1:1", [task.task_key for task in viewport_tasks])
        self.assertTrue(all(task.payload["refresh_requests"][0]["trigger"] == "viewport" for task in viewport_tasks))

    def test_history_refresh_uses_page_context_for_non_visible_buckets(self) -> None:
        show_key = "03.04.2026:show:1"
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": [],
                "nearby_title_keys": [show_key],
                "page_title_keys": [show_key],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(task.payload["refresh_requests"][0]["trigger"] == TRIGGER_PAGE_CONTEXT for task in self.enrich_queue.submissions[0]["nearby"]))
        self.assertTrue(all(task.payload["refresh_requests"][0]["trigger"] == TRIGGER_PAGE_CONTEXT for task in self.enrich_queue.submissions[0]["page"]))

    def test_history_refresh_title_mode_patches_title_cards_without_episode_tasks(self) -> None:
        self.enrich_queue.updates = [
            {
                "revision": 1,
                "task_key": "title:show:1",
                "kind": "history_title",
                "status": TASK_STATUS_COMPLETED,
                "result": "ready",
                "affected_title_keys": ["show:1"],
            }
        ]
        response = self.client.post(
            "/history/refresh",
            json={
                "type": "all",
                "view": "titles",
                "title_filter": "",
                "page": 1,
                "viewport_title_keys": ["show:1"],
                "nearby_title_keys": [],
                "page_title_keys": ["show:1", "movie:2"],
                "queue_after_revision": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["title_key"] for item in payload["title_groups"]], ["show:1"])
        self.assertIn("history-title-mode-card", payload["title_groups"][0]["html"])
        self.assertEqual([task.task_key for task in self.enrich_queue.submissions[0]["viewport"]], ["title:show:1"])

    def test_history_page_rated_only_filters_out_unrated_and_movies(self) -> None:
        self.history.rows = [
            {
                **self._row("show", 1, "Severance", watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC)),
                "display_rating": 8,
            },
            self._row("show", 3, "Unrated Show", watched_at=datetime(2026, 4, 3, 10, 0, tzinfo=UTC)),
            {
                **self._row("movie", 2, "Dune", watched_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC)),
                "display_rating": 9,
            },
        ]
        response = self.client.get("/history?rated_only=1")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Severance", html)
        self.assertIn('data-history-title-key="03.04.2026:show:1"', html)
        self.assertNotIn('data-history-title-key="03.04.2026:show:3"', html)
        self.assertNotIn('data-history-title-key="03.04.2026:movie:2"', html)

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
            "watched_at_known": True,
            "season": 1 if title_type == "show" else None,
            "episode": 1 if title_type == "show" else None,
            "episode_title": "Episode 1" if title_type == "show" else None,
            "episode_still_url": "",
            "episode_still_status": "unknown",
            "episode_still_refreshed_at": None,
            "episode_trakt_rating": None,
            "episode_trakt_votes": None,
            "episode_trakt_status": "unknown",
            "episode_imdb_rating": None,
            "episode_imdb_votes": None,
            "episode_imdb_status": "unknown",
            "episode_first_aired": None,
            "event_rating": None,
            "title_rating": None,
            "display_rating": None,
        }


if __name__ == "__main__":
    unittest.main()
