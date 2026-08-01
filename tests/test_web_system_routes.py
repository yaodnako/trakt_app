from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import SyncStateRepository
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.web.routes_system import register_system_routes

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WebSystemRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "templates"
        static_dir = PROJECT_ROOT / "trakt_tracker" / "web" / "static"
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.app.state.services = SimpleNamespace(
            auth=SimpleNamespace(
                config=SimpleNamespace(
                    cache_ttl_hours=24,
                    explore_imdb_scan_page_limit=10,
                    notifications_enabled=False,
                    debug_mode=False,
                    open_in_embedded_player=False,
                    utc_offset="+03:00",
                    poll_interval_minutes=30,
                    imdb_auto_sync_interval_hours=3,
                    tmdb_api_key="test-key",
                    tmdb_read_access_token="",
                ),
                update_config=lambda *args, **kwargs: SimpleNamespace(),
                authorize=lambda: "test-user",
                is_authorized=lambda: True,
                is_configured=lambda: True,
            ),
            sync=SimpleNamespace(imdb_dataset_status=lambda: "ready", sync_assets_repair=lambda: None),
            notifications=SimpleNamespace(
                poll_upcoming=lambda send_native=False, refresh_remote=True: [],
                record_activity=lambda items: 7,
                activity_after=lambda after=0: (
                    [{"seq": 7, "sources": ["progress"]}] if after < 7 else []
                ),
                current_activity_seq=lambda: 7,
                pending_sources=lambda: ["progress"],
            ),
            operations=SimpleNamespace(list_after=lambda after=0: []),
            enrich_queue=SimpleNamespace(is_running=lambda: False),
        )
        self.image_jobs: list[tuple[str, int]] = []
        self.app.state.services.image_queue = SimpleNamespace(
            submit=lambda url, *, priority: self.image_jobs.append((url, priority)) or True,
        )
        self.started: list[tuple[str, str]] = []
        self.app.state.bg_tasks = SimpleNamespace(
            is_running=lambda key: False,
            start=lambda key, source, operations, fn: self.started.append((key, source)) or True,
        )
        self.app.state.image_cache = BinaryCache("images_test_routes")
        self.app.state.image_cache.clear()

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

        register_system_routes(self.app, render=render, template_filters=SimpleNamespace(utc_offset="+03:00"))
        self.client = TestClient(self.app)

    def test_cached_image_returns_pending_placeholder_and_queues_cache_miss(self) -> None:
        target_url = "https://image.tmdb.org/t/p/w342/image.jpg"
        response = self.client.get("/cached-image", params={"url": target_url}, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/gif")
        self.assertEqual(response.headers["x-trakt-image-pending"], "1")
        self.assertEqual(self.image_jobs, [(target_url, 1)])

    def test_notification_activity_exposes_only_new_local_delivery_events(self) -> None:
        self.app.state.services.auth.is_authorized = lambda: False

        response = self.client.get("/notifications/activity", params={"after": 6})

        self.assertEqual(
            response.json(),
            {
                "events": [{"seq": 7, "sources": ["progress"]}],
                "seq": 7,
                "pending_sources": ["progress"],
            },
        )

    def test_browser_notification_poll_returns_recorded_activity_sequence(self) -> None:
        calls: list[dict] = []
        self.app.state.services.auth.is_authorized = lambda: False
        self.app.state.services.notifications.poll_upcoming = (
            lambda **kwargs: calls.append(kwargs)
            or [{"show_title": "Show", "message": "S01E02", "source": "progress"}]
        )

        response = self.client.get("/notifications/poll")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["activity_seq"], 7)
        self.assertEqual(response.json()["items"][0]["source"], "progress")
        self.assertEqual(calls, [{"send_native": False, "refresh_remote": False}])

    def test_notification_nav_state_is_not_cleared_by_navigation(self) -> None:
        script = (PROJECT_ROOT / "trakt_tracker" / "web" / "static" / "ui_core.js").read_text(
            encoding="utf-8"
        )
        styles = (PROJECT_ROOT / "trakt_tracker" / "web" / "static" / "style.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('runtimeValue("trakt-notification-pending-sources")', script)
        self.assertIn("setPendingNotificationSources", script)
        self.assertNotIn("clearCurrentNotificationSource", script)
        self.assertNotIn("trakt-notification-nav-unread-v1", script)
        self.assertIn("notification-badge-heartbeat", styles)
        self.assertIn("notification-badge-ripple", styles)

    def test_cached_image_rejects_arbitrary_and_private_urls_before_fetch(self) -> None:
        arbitrary = self.client.get("/cached-image", params={"url": "https://example.com/image.jpg"})
        loopback = self.client.get("/cached-image", params={"url": "https://127.0.0.1/image.jpg"})
        insecure = self.client.get("/cached-image", params={"url": "http://image.tmdb.org/t/p/w342/image.jpg"})
        self.assertEqual(arbitrary.status_code, 400)
        self.assertEqual(loopback.status_code, 400)
        self.assertEqual(insecure.status_code, 400)

    def test_cached_image_serves_fresh_payload_without_duplicating_queue_work(self) -> None:
        target_url = "https://image.tmdb.org/t/p/w342/image.jpg"
        self.app.state.image_cache.set_bytes(target_url, b"\xff\xd8\xffpayload", suffix=".jpg")
        response = self.client.get("/cached-image", params={"url": target_url}, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\xff\xd8\xffpayload")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(self.image_jobs, [])

    def test_cached_image_returns_cached_payload_with_no_store_header(self) -> None:
        target_url = "https://image.tmdb.org/t/p/w342/cached.png"
        self.app.state.image_cache.set_bytes(target_url, b"\x89PNG\r\n\x1a\npayload", suffix=".png")
        response = self.client.get(
            "/cached-image",
            params={"url": target_url},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\x89PNG\r\n\x1a\npayload")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_cached_image_detects_webp_payload_for_jpg_url(self) -> None:
        target_url = "https://image.tmdb.org/t/p/w342/image.jpg"
        webp_payload = b"RIFF1234WEBPpayload"
        self.app.state.image_cache.set_bytes(target_url, webp_payload, suffix=".jpg")
        response = self.client.get(
            "/cached-image",
            params={"url": target_url},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, webp_payload)
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_settings_sync_repair_starts_background_task(self) -> None:
        response = self.client.post("/settings/sync-repair", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/settings?flash=Metadata%20recheck%20started.", response.headers["location"])
        self.assertEqual(self.started, [("settings_repair_sync", "Metadata recheck")])

    def test_settings_template_separates_save_and_operation_forms(self) -> None:
        template = (PROJECT_ROOT / "trakt_tracker" / "web" / "templates" / "settings.html").read_text(
            encoding="utf-8"
        )
        script = (PROJECT_ROOT / "trakt_tracker" / "web" / "static" / "settings_page.js").read_text(
            encoding="utf-8"
        )
        routes = (PROJECT_ROOT / "trakt_tracker" / "web" / "routes_system.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-settings-save-bar', template)
        self.assertIn('name="web_hide_spoilers"', template)
        self.assertIn("Don't show spoilers", template)
        self.assertIn(
            'config.web_hide_spoilers = parse_bool_flag(str(form.get("web_hide_spoilers", "")))',
            routes,
        )
        self.assertIn('form method="post" action="/settings/full-sync"', template)
        self.assertIn('data-sync-task="full_sync"', template)
        self.assertNotIn('formaction="/settings/full-sync"', template)
        self.assertIn('button.textContent = active ? "Running" : waiting ? "Queued"', script)

    def test_settings_can_authorize_trakt_without_desktop_ui(self) -> None:
        response = self.client.post("/settings/trakt-authorize", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertIn("/settings?flash=Trakt%20authorized%20as%20test-user.", response.headers["location"])

    def test_refresh_status_exposes_provider_queue_and_artwork_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "status.sqlite3")
            db.create_schema()
            sync_state = SyncStateRepository()
            with db.session() as session:
                sync_state.set_value(session, SyncPolicy.HISTORY_LAST_SYNC_KEY, "2026-07-16T10:00:00+00:00")
                sync_state.set_value(session, SyncPolicy.HISTORY_LAST_FULL_RECONCILE_KEY, "2026-07-16T09:30:00+00:00")
            self.app.state.services.sync._db = db
            self.app.state.services.sync._sync_state = sync_state
            self.app.state.services.enrich_queue.status_snapshot = lambda: {
                "pending": 2, "running": 1, "cooldown": 3, "failed": 4, "last_failure": None,
            }
            self.app.state.artwork_cache_warm_loop = SimpleNamespace(
                status_snapshot=lambda: {"status": "partial", "at": "2026-07-16T10:01:00+00:00", "failed": 1}
            )

            response = self.client.get("/settings/refresh-status")
            db.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["trakt"]["authorized"])
        self.assertTrue(payload["tmdb"]["configured"])
        self.assertEqual(payload["history"]["last_success_at"], "2026-07-16T10:00:00+00:00")
        self.assertEqual(payload["queue"]["cooldown"], 3)
        self.assertEqual(payload["artwork"]["failed"], 1)
        self.assertFalse(payload["queued"]["repair_sync"])


if __name__ == "__main__":
    unittest.main()
