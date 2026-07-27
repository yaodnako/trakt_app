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

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeProgressService:
    def __init__(self, items: list[ProgressSnapshot]) -> None:
        self.items = items
        self.dashboard_calls: list[dict] = []
        self.sync_calls: list[dict] = []

    def dashboard_progress(
        self,
        *,
        view="active",
        sort_mode="episode_release",
        descending=True,
        dropped_only=None,
        limit=50,
    ):
        view_value = getattr(view, "value", view)
        if dropped_only is not None:
            view_value = "dropped" if dropped_only else "active"
        self.dashboard_calls.append(
            {
                "view": view_value,
                "sort_mode": getattr(sort_mode, "value", sort_mode),
                "descending": descending,
            }
        )
        if view_value == "dropped":
            return [item for item in self.items if item.is_dropped]
        if view_value == "paused":
            return [item for item in self.items if item.is_paused and not item.is_dropped]
        return [item for item in self.items if not item.is_paused and not item.is_dropped]

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

    def sync_progress(
        self,
        trakt_ids=None,
        *,
        view="active",
        sort_mode="episode_release",
        descending=True,
        dropped_only=None,
        force_refresh=False,
        force_full_assets=False,
    ):
        self.sync_calls.append(
            {
                "trakt_ids": list(trakt_ids or []),
                "view": getattr(view, "value", view),
                "sort_mode": getattr(sort_mode, "value", sort_mode),
                "descending": descending,
                "force_refresh": force_refresh,
                "force_full_assets": force_full_assets,
            }
        )
        return []


class _FakeInteractionService:
    def __init__(self) -> None:
        self.pause_calls: list[dict] = []
        self.drop_calls: list[dict] = []
        self.watch_calls: list[dict] = []

    def set_progress_paused(self, trakt_id: int, *, paused: bool, progress=None) -> None:
        self.pause_calls.append({"trakt_id": trakt_id, "paused": paused, "progress": progress})

    def set_progress_dropped(self, trakt_id: int, *, dropped: bool) -> None:
        self.drop_calls.append({"trakt_id": trakt_id, "dropped": dropped})

    def mark_progress_episode_watched(self, progress, *, watched_at) -> None:
        self.watch_calls.append({"trakt_id": progress.trakt_id, "watched_at": watched_at})


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
        templates_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        static_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "static"
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["dt"] = lambda value: value.isoformat() if value else ""
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
                    imdb_season=3,
                    imdb_episode=1,
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
        self.interactions = _FakeInteractionService()
        self.queue = _FakeEnrichQueueService()
        self.unseen_episode_ids: set[int] = set()
        self.watchlist_calls: list[dict] = []
        self.watchlist_keys = {("show", 1)}
        self.app.state.services = SimpleNamespace(
            progress=self.progress,
            enrich_queue=self.queue,
            notifications=SimpleNamespace(unseen_episode_ids=lambda: self.unseen_episode_ids),
            auth=SimpleNamespace(
                config=SimpleNamespace(
                    hide_upcoming_in_progress=False,
                    show_paused_in_progress=False,
                    show_dropped_in_progress=False,
                    web_progress_sort_mode="episode_release",
                    web_progress_sort_direction="desc",
                    web_progress_min_year=None,
                    web_progress_year_filter_enabled=False,
                    web_hide_spoilers=False,
                    active_slug="test-user",
                ),
            ),
            interactions=self.interactions,
            catalog=SimpleNamespace(
                watchlist_keys=lambda *, title_type=None: {
                    key for key in self.watchlist_keys if title_type is None or key[0] == title_type
                },
                set_watchlisted=lambda title_type, trakt_id, *, watchlisted: self.watchlist_calls.append(
                    {"title_type": title_type, "trakt_id": trakt_id, "watchlisted": watchlisted}
                ),
            ),
            play=SimpleNamespace(),
            operations=SimpleNamespace(publish=lambda *args: None),
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
                "active_profile_slug": self.app.state.services.auth.config.active_slug,
                "web_hide_spoilers": self.app.state.services.auth.config.web_hide_spoilers,
            }
            base_context.update(context)
            return self.templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

        def progress_redirect(**kwargs) -> RedirectResponse:
            return RedirectResponse(url=f"/progress?{progress_query_string(**kwargs)}", status_code=303)

        self.app.state.render_fragment = lambda request, template_name, context: self.templates.get_template(template_name).render(
            {
                "request": request,
                "current_path": request.url.path,
                "active_profile_slug": self.app.state.services.auth.config.active_slug,
                "web_hide_spoilers": self.app.state.services.auth.config.web_hide_spoilers,
                **context,
            }
        )
        register_progress_routes(self.app, render=render, progress_redirect=progress_redirect)
        self.client = TestClient(self.app)

    def test_progress_poster_opens_episode_panel_and_title_links_to_trakt(self) -> None:
        response = self.client.get("/progress")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("<title>Up next | Trakt Tracker Web</title>", html)
        self.assertIn('href="/progress" class="active" aria-current="page" data-notification-source="progress"', html)
        self.assertIn('<h3><a href="https://trakt.tv/shows/1" target="_blank" rel="noreferrer">Severance</a></h3>', html)
        self.assertIn('data-watch-panel-url="/search/show/1/watch-panel"', html)
        self.assertIn('data-watch-panel-url="/search/show/2/watch-panel"', html)
        self.assertIn('data-trakt-id="1"', html)
        self.assertIn('data-trakt-id="2"', html)
        self.assertIn('id="history-watch-overlay"', html)
        self.assertIn("data-show-watch-play", html)
        watch_script = (
            PROJECT_ROOT / "trakt_tracker" / "web" / "static" / "history_watch_panel.js"
        ).read_text(encoding="utf-8")
        self.assertIn("configurePlayAction(watchOverlay, trigger)", watch_script)
        self.assertIn("show_watch_panel.js?v=", html)
        self.assertIn("Episode Watch", html)
        self.assertIn("data-confirm-message=", html)
        self.assertIn("Remove from Up next?", html)
        self.assertIn('action="/progress/1/pause-toggle"', html)
        self.assertIn("more_options.svg", html)
        self.assertIn("pause.svg", html)
        self.assertLess(html.index("/progress/1/pause-toggle"), html.index("more_options.svg"))
        self.assertLess(html.index("more_options.svg"), html.index("/progress/1/drop-toggle"))
        self.assertIn('<option value="episode_release" selected>Episode release</option>', html)
        self.assertIn("data-progress-sort-direction", html)
        footer_start = html.index('<div class="progress-actions">')
        footer_end = html.index("</div>", footer_start)
        self.assertNotIn("/drop-toggle", html[footer_start:footer_end])
        self.assertIn("S02E03 (S03E01)", html)
        self.assertNotIn("return confirm(", html)
        self.assertNotIn("scheduleWatchPanelRefreshIfPending", html)
        self.assertIn("refreshWatchPanel", watch_script)

    def test_progress_spoilers_blur_only_real_next_episode_stills(self) -> None:
        config = self.app.state.services.auth.config
        config.web_hide_spoilers = True
        first = self.progress.items[0]
        second = self.progress.items[1]
        first.next_episode.still_url = "https://still.example/severance.jpg"
        second.poster_url = "https://poster.example/andor.jpg"

        protected = self.client.get("/progress").text

        self.assertIn('data-spoiler-key="test-user:1:2:3"', protected)
        self.assertEqual(protected.count("Click to unblur"), 1)
        self.assertNotIn('data-spoiler-key="test-user:2:1:4"', protected)

        config.web_hide_spoilers = False
        unprotected = self.client.get("/progress").text
        self.assertNotIn("data-spoiler-key", unprotected)

    def test_progress_episode_watch_removes_watchlisted_show(self) -> None:
        response = self.client.post("/progress/1/watch", data={}, follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(self.interactions.watch_calls), 1)
        self.assertEqual(
            self.watchlist_calls,
            [{"title_type": "show", "trakt_id": 1, "watchlisted": False}],
        )

    def test_progress_toolbar_uses_compact_state_and_sort_controls(self) -> None:
        response = self.client.get("/progress")

        self.assertEqual(response.status_code, 200)
        toolbar_start = response.text.index('<div class="progress-toolbar">')
        toolbar_end = response.text.index("</section>", toolbar_start)
        toolbar = response.text[toolbar_start:toolbar_end]
        self.assertIn("static/pause.svg", toolbar)
        self.assertIn("<span>Paused</span>", toolbar)
        self.assertIn("static/drop_red.svg", toolbar)
        self.assertIn("<span>Dropped</span>", toolbar)
        self.assertNotIn("Show Paused", toolbar)
        self.assertNotIn("Show Dropped", toolbar)
        self.assertIn('data-progress-sort-mode="episode_release"', toolbar)

        css = (PROJECT_ROOT / "trakt_tracker" / "web" / "static" / "style.css").read_text(encoding="utf-8")
        self.assertNotIn("field-sizing: content;", css)
        self.assertIn('select[data-progress-sort-mode="episode_release"] {\n    width: 136px;', css)
        self.assertIn("width: 58px;\n    min-width: 58px;", css)
        self.assertIn("gap: 3px;", css)
        self.assertIn(".progress-filter-icon-pause {\n    width: 12px;", css)
        self.assertIn(".progress-filter-icon-drop {\n    width: 17.142857px;", css)
        self.assertIn("margin-inline: -2.571429px;", css)

    def test_progress_filters_normalize_to_dropped_and_forward_sort_state(self) -> None:
        self.progress.items = [
            ProgressSnapshot(trakt_id=1, title="Active", completed=1, aired=2, percent_completed=50.0),
            ProgressSnapshot(trakt_id=2, title="Paused", completed=1, aired=2, percent_completed=50.0, is_paused=True),
            ProgressSnapshot(
                trakt_id=3,
                title="Dropped",
                completed=1,
                aired=2,
                percent_completed=50.0,
                is_dropped=True,
                is_paused=True,
            ),
        ]
        config = self.app.state.services.auth.config
        config.show_paused_in_progress = False
        config.show_dropped_in_progress = True
        config.web_progress_sort_mode = "last_watched"
        config.web_progress_sort_direction = "asc"

        response = self.client.get(
            "/progress?show_paused=1&show_dropped=1&sort=Last+watched&direction=asc"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dropped", response.text)
        self.assertNotIn(">Active<", response.text)
        self.assertNotIn('data-progress-card-key="progress:2"', response.text)
        self.assertIn('data-show-paused="0"', response.text)
        self.assertIn('data-show-dropped="1"', response.text)
        self.assertIn('class="progress-card is-dropped"', response.text)
        self.assertNotIn('class="progress-card is-paused"', response.text)
        self.assertEqual(
            self.progress.dashboard_calls[-1],
            {"view": "dropped", "sort_mode": "last_watched", "descending": False},
        )
        self.assertNotIn("/pause-toggle", response.text)
        self.assertIn('title="Undrop"', response.text)

    def test_progress_paused_view_renders_resume_before_drop(self) -> None:
        self.progress.items = [
            ProgressSnapshot(
                trakt_id=2,
                title="Paused",
                completed=1,
                aired=2,
                percent_completed=50.0,
                is_paused=True,
            ),
        ]
        self.app.state.services.auth.config.show_paused_in_progress = True

        response = self.client.get("/progress?show_paused=1&show_dropped=0")

        self.assertEqual(response.status_code, 200)
        self.assertIn("resume.svg", response.text)
        self.assertIn('title="Resume"', response.text)
        self.assertIn('title="Drop"', response.text)

    def test_progress_pause_toggle_calls_remote_first_interaction_and_preserves_state(self) -> None:
        response = self.client.post(
            "/progress/1/pause-toggle",
            data={
                "hide_upcoming": "1",
                "show_paused": "0",
                "show_dropped": "0",
                "sort": "last_watched",
                "direction": "asc",
                "min_year": "2020",
                "use_year_filter": "1",
                "is_paused": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            self.interactions.pause_calls,
            [{"trakt_id": 1, "paused": True, "progress": self.progress.items[0]}],
        )
        location = response.headers["location"]
        self.assertIn("show_paused=0", location)
        self.assertIn("sort=last_watched", location)
        self.assertIn("direction=asc", location)
        self.assertIn("min_year=2020", location)

    def test_progress_resume_toggle_uses_paused_bucket(self) -> None:
        self.progress.items[0].is_paused = True

        response = self.client.post(
            "/progress/1/pause-toggle",
            data={
                "show_paused": "1",
                "show_dropped": "0",
                "sort": "episode_release",
                "direction": "desc",
                "is_paused": "1",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            self.interactions.pause_calls,
            [{"trakt_id": 1, "paused": False, "progress": self.progress.items[0]}],
        )
        self.assertEqual(self.progress.dashboard_calls[-1]["view"], "paused")

    def test_progress_new_items_remain_first_without_changing_bucket_order(self) -> None:
        regular = self.progress.items[0]
        new_item = self.progress.items[1]
        self.progress.items = [regular, new_item]
        self.unseen_episode_ids = {new_item.next_episode.trakt_id}

        response = self.client.get("/progress")

        self.assertEqual(response.status_code, 200)
        self.assertLess(response.text.index("Andor"), response.text.index("Severance"))
        self.assertIn("progress-new-badge", response.text)

    def test_progress_new_partition_is_applied_before_page_limit(self) -> None:
        released_at = datetime.now(tz=UTC) - timedelta(days=1)
        self.progress.items = [
            ProgressSnapshot(
                trakt_id=1000 + offset,
                title=f"Show {offset:02d}",
                completed=1,
                aired=2,
                percent_completed=50.0,
                next_episode=EpisodeSummary(
                    trakt_id=2000 + offset,
                    season=1,
                    number=2,
                    title="Next",
                    first_aired=released_at,
                ),
            )
            for offset in range(51)
        ]
        self.unseen_episode_ids = {2050}

        response = self.client.get("/progress")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertEqual(html.count('data-progress-card-key="progress:'), 50)
        self.assertLess(
            html.index('data-progress-card-key="progress:1050"'),
            html.index('data-progress-card-key="progress:1000"'),
        )
        self.assertNotIn('data-progress-card-key="progress:1049"', html)

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
        self.assertNotIn('action="/progress/sync"', html)
        self.assertNotIn(">Sync</button>", html)

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
