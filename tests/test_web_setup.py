from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.infrastructure.trakt.client import TraktReauthorizationRequired
from trakt_tracker.profiles import mark_setup_complete, read_setup_state
from trakt_tracker.web.app import create_app
from trakt_tracker.web_tray import TrayNotificationPoller, WebPortalTrayWindow


def _app(tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    store.save(
        AppConfig(
            client_id="client",
            client_secret="secret",
            database_path=str(tmp_path / "legacy.sqlite3"),
            active_profile_slug="alpha",
            known_profile_slugs=["alpha", "beta"],
            legacy_profile_migrated_slug="alpha",
        )
    )
    app = create_app(store)
    return app, store


def _csrf_headers(client: TestClient) -> dict[str, str]:
    client.get("/setup")
    return {"X-Trakt-CSRF": client.cookies["trakt_csrf"]}


def test_setup_gate_returns_redirect_401_and_409(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    app.state.services.auth.is_authorized = lambda: False
    try:
        html = client.get("/progress", follow_redirects=False)
        unauthorized_json = client.get(
            "/progress",
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        app.state.services.auth.is_authorized = lambda: True
        incomplete_json = client.get(
            "/progress",
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
    finally:
        app.state.runtime.close()

    assert html.status_code == 302
    assert html.headers["location"] == "/setup"
    assert unauthorized_json.status_code == 401
    assert incomplete_json.status_code == 409


def test_completed_profile_without_token_shows_direct_token_update(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    mark_setup_complete(app.state.services.database)
    app.state.services.auth.is_authorized = lambda: False
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        html = client.get("/search?q=Fullmetal+Alchemist&type=show", follow_redirects=False)
        api = client.get(
            "/notifications/poll",
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        partial = client.get(
            "/search?q=Fullmetal+Alchemist&type=show",
            headers={"Accept": "text/html", "X-Trakt-Partial": "catalog"},
            follow_redirects=False,
        )
    finally:
        app.state.runtime.close()

    assert html.status_code == 401
    assert "location" not in html.headers
    assert "Update your Trakt token" in html.text
    assert 'action="/settings/trakt-authorize"' in html.text
    assert 'value="/search?q=Fullmetal+Alchemist&amp;type=show"' in html.text
    assert api.status_code == 401
    assert api.json()["code"] == "trakt_reauth_required"
    assert api.json()["return_to"] == "/notifications/poll"
    assert partial.status_code == 401
    assert partial.json()["code"] == "trakt_reauth_required"
    assert partial.json()["return_to"] == "/search?q=Fullmetal+Alchemist&type=show"


def test_request_that_loses_authorization_shows_token_update_instead_of_provider_error(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    mark_setup_complete(app.state.services.database)
    authorized = {"value": True}
    app.state.services.auth.is_authorized = lambda: authorized["value"]

    @app.get("/reauth-probe")
    async def reauth_probe():
        authorized["value"] = False
        return {"provider_error": "session not found"}

    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        response = client.get("/reauth-probe", follow_redirects=False)
    finally:
        app.state.runtime.close()

    assert response.status_code == 401
    assert "Update your Trakt token" in response.text
    assert "session not found" not in response.text


def test_reauthorization_exception_shows_token_update_instead_of_server_error(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    mark_setup_complete(app.state.services.database)
    app.state.services.auth.is_authorized = lambda: True

    @app.get("/reauth-exception-probe")
    async def reauth_exception_probe():
        raise TraktReauthorizationRequired("Reconnect required")

    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        response = client.get("/reauth-exception-probe", follow_redirects=False)
    finally:
        app.state.runtime.close()

    assert response.status_code == 401
    assert "Update your Trakt token" in response.text


def test_reconnect_returns_to_safe_requested_page(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    mark_setup_complete(app.state.services.database)
    original_services = app.state.services
    app.state.services.auth.authorize = lambda: "alpha"
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        settings = client.get("/settings?next=/search%3Fq%3DFullmetal%2BAlchemist")
        response = client.post(
            "/settings/trakt-authorize",
            data={"return_to": "/search?q=Fullmetal+Alchemist"},
            follow_redirects=False,
        )
    finally:
        app.state.runtime.close()

    assert 'name="return_to" value="/search?q=Fullmetal+Alchemist"' in settings.text
    assert response.status_code == 303
    assert response.headers["location"].startswith("/search?q=Fullmetal+Alchemist&flash=")
    assert app.state.services is original_services


def test_setup_page_renders_sign_in_without_saved_secret(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    app.state.services.auth.is_authorized = lambda: False
    try:
        response = TestClient(app, base_url="http://127.0.0.1").get("/setup")
    finally:
        app.state.runtime.close()

    assert response.status_code == 200
    assert "Sign in with Trakt" in response.text
    assert "secret" not in response.text


def test_completed_profile_opens_portal_and_setup_redirects_to_progress(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    app.state.services.auth.is_authorized = lambda: True
    mark_setup_complete(app.state.services.database)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        setup_response = client.get("/setup", follow_redirects=False)
        progress_response = client.get("/progress")
    finally:
        app.state.runtime.close()

    assert setup_response.status_code == 302
    assert setup_response.headers["location"] == "/progress"
    assert progress_response.status_code == 200


def test_setup_status_shape_and_sync_start(tmp_path: Path) -> None:
    app, _store = _app(tmp_path)
    services = app.state.services
    services.auth.is_authorized = lambda: True
    started: list[str] = []
    app.state.bg_tasks = SimpleNamespace(
        is_running=lambda _key: False,
        start=lambda key, source, operations, fn: started.append(key) or True,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        status = client.get("/setup/status").json()
        sync = client.post("/setup/sync").json()
    finally:
        app.state.runtime.close()

    assert status == {
        "configured": True,
        "authorized": True,
        "profile_slug": "alpha",
        "state": "pending",
        "stage": "history",
        "message": "Ready to sync Trakt data.",
        "error": "",
        "running": False,
    }
    assert sync["started"] is True
    assert started == ["initial_setup:alpha"]


def test_profile_switch_is_blocked_during_background_work(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    services = app.state.services
    services.auth.has_token = lambda _slug: True
    app.state.bg_tasks = SimpleNamespace(
        any_running=lambda: True,
        is_running=lambda _key: False,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        response = client.post(
            "/settings/profiles/switch",
            data={"slug": "beta"},
            follow_redirects=False,
        )
    finally:
        app.state.runtime.close()

    assert response.status_code == 303
    assert "blocked" in response.headers["location"]
    assert store.load().active_slug == "alpha"


def test_profile_switch_activates_saved_profile(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    app.state.services.auth.has_token = lambda _slug: True
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        response = client.post(
            "/settings/profiles/switch",
            data={"slug": "beta"},
            follow_redirects=False,
        )
        beta_state = read_setup_state(app.state.services.database)
    finally:
        app.state.runtime.close()

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert store.load().active_slug == "beta"
    assert beta_state["state"] == "pending"


def test_successful_authorization_activates_profile_and_starts_setup(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    app.state.services.auth.authorize = lambda: "new-viewer"
    started: list[str] = []
    app.state.bg_tasks = SimpleNamespace(
        is_running=lambda _key: False,
        start=lambda key, source, operations, fn: started.append(key) or True,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        response = client.post(
            "/settings/trakt-authorize",
            data={"return_to": "/setup"},
            follow_redirects=False,
        )
    finally:
        app.state.runtime.close()

    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?flash=")
    assert store.load().active_slug == "new-viewer"
    assert started == ["initial_setup:new-viewer"]


def test_disconnect_preserves_active_profile_and_redirects_to_setup(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    disconnected: list[str] = []
    app.state.services.auth.disconnect = disconnected.append
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update(_csrf_headers(client))
    try:
        response = client.post(
            "/settings/profiles/disconnect",
            data={"slug": "alpha"},
            follow_redirects=False,
        )
    finally:
        app.state.runtime.close()

    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?flash=")
    assert disconnected == ["alpha"]
    assert store.load().active_slug == "alpha"


def test_tray_poller_refreshes_profile_before_polling() -> None:
    calls: list[str] = []
    services = SimpleNamespace(
        auth=SimpleNamespace(is_authorized=lambda: calls.append("authorized") or False),
    )
    runtime = SimpleNamespace(
        active_slug="beta",
        services=services,
        refresh_active_profile=lambda: calls.append("refresh") or True,
    )
    poller = TrayNotificationPoller(runtime)

    poller._run()

    assert calls == ["refresh", "authorized"]


def test_tray_poller_does_not_duplicate_progress_sync_owned_by_notification_service() -> None:
    calls: list[str] = []
    items = [{"show_title": "Show", "message": "S01E02", "source": "progress"}]
    services = SimpleNamespace(
        auth=SimpleNamespace(is_authorized=lambda: calls.append("authorized") or True),
        notifications=SimpleNamespace(
            poll_upcoming=lambda send_native=True: calls.append("poll") or items
        ),
        progress=SimpleNamespace(
            sync_progress=lambda dropped_only=False: calls.append("progress")
        ),
    )
    runtime = SimpleNamespace(
        active_slug="alpha",
        services=services,
        refresh_active_profile=lambda: calls.append("refresh") or False,
    )
    poller = TrayNotificationPoller(runtime)
    poller.notificationsReceived.connect(lambda delivered: calls.append("emit"))

    poller._run()

    assert calls == ["refresh", "authorized", "poll", "emit"]


def test_tray_notification_activity_is_recorded_before_sound() -> None:
    calls: list[object] = []
    items = [{"show_title": "Show", "message": "S01E02", "source": "progress"}]
    window = SimpleNamespace(
        _append_log=lambda message: calls.append(message),
        _runtime=SimpleNamespace(
            services=SimpleNamespace(
                notifications=SimpleNamespace(record_activity=lambda delivered: calls.append(delivered))
            )
        ),
        _play_notification_sound=lambda: calls.append("sound"),
    )

    WebPortalTrayWindow._on_notifications_received(window, items)

    assert calls == ["notifications received: 1", items, "sound"]
