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
            sync=SimpleNamespace(imdb_dataset_status=lambda: "ready"),
            notifications=SimpleNamespace(poll_upcoming=lambda send_native=False: []),
            operations=SimpleNamespace(list_after=lambda after=0: []),
        )
        self.app.state.bg_tasks = SimpleNamespace(
            is_running=lambda key: False,
            start=lambda key, source, operations, fn: False,
        )
        self.app.state.image_cache = BinaryCache("images_test_routes")

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

    def test_cached_image_redirects_immediately_on_cache_miss(self) -> None:
        called: list[str] = []
        original = routes_system._warm_image_cache_in_background
        routes_system._warm_image_cache_in_background = lambda cache, target_url, timeout=5: called.append(target_url)
        try:
            response = self.client.get(
                "/cached-image",
                params={"url": "https://example.com/image.jpg"},
                follow_redirects=False,
            )
        finally:
            routes_system._warm_image_cache_in_background = original
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://example.com/image.jpg")
        self.assertEqual(called, ["https://example.com/image.jpg"])


if __name__ == "__main__":
    unittest.main()
