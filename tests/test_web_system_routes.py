from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import trakt_tracker.web.routes_system as routes_system
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.web.routes_system import register_system_routes


class WebSystemRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        templates_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/templates")
        static_dir = Path("D:/CodexProjects/Trakt_app/trakt_tracker/web/static")
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        self.app.state.services = SimpleNamespace(
            auth=SimpleNamespace(
                config=SimpleNamespace(
                    cache_ttl_hours=24,
                    notifications_enabled=False,
                    debug_mode=False,
                    open_in_embedded_player=False,
                    utc_offset="+03:00",
                    poll_interval_minutes=30,
                    imdb_auto_sync_interval_hours=3,
                ),
                update_config=lambda *args, **kwargs: SimpleNamespace(),
                is_authorized=lambda: True,
                is_configured=lambda: True,
            ),
            sync=SimpleNamespace(imdb_dataset_status=lambda: "ready", sync_assets_repair=lambda: None),
            notifications=SimpleNamespace(poll_upcoming=lambda send_native=False: []),
            operations=SimpleNamespace(list_after=lambda after=0: []),
            enrich_queue=SimpleNamespace(is_running=lambda: False),
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

    def test_cached_image_returns_error_and_warms_cache_on_fetch_miss(self) -> None:
        original_fetch = routes_system.fetch_and_cache_image
        original_warm = routes_system.warm_image_cache_in_background
        warmed: list[tuple[str, int]] = []
        routes_system.fetch_and_cache_image = lambda cache, target_url, timeout: None
        routes_system.warm_image_cache_in_background = lambda cache, target_url, timeout: warmed.append((target_url, timeout))
        try:
            response = self.client.get(
                "/cached-image",
                params={"url": "https://example.com/image.jpg"},
                follow_redirects=False,
            )
        finally:
            routes_system.fetch_and_cache_image = original_fetch
            routes_system.warm_image_cache_in_background = original_warm
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("location", response.headers)
        self.assertEqual(warmed, [("https://example.com/image.jpg", 5)])

    def test_cached_image_returns_fetched_payload_on_cache_miss(self) -> None:
        original_fetch = routes_system.fetch_and_cache_image
        routes_system.fetch_and_cache_image = lambda cache, target_url, timeout: (b"\xff\xd8\xffpayload", "image/jpeg")
        try:
            response = self.client.get(
                "/cached-image",
                params={"url": "https://example.com/image.jpg"},
                follow_redirects=False,
            )
        finally:
            routes_system.fetch_and_cache_image = original_fetch
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\xff\xd8\xffpayload")
        self.assertEqual(response.headers["content-type"], "image/jpeg")

    def test_cached_image_returns_cached_payload_with_no_store_header(self) -> None:
        target_url = "https://example.com/cached.png"
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
        target_url = "https://example.com/image.jpg"
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
        self.assertIn("/settings?flash=Repair%20sync%20started.", response.headers["location"])
        self.assertEqual(self.started, [("settings_repair_sync", "Repair sync")])


if __name__ == "__main__":
    unittest.main()
