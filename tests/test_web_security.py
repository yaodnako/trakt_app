from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.web.app import create_app
from trakt_tracker.web.security import portal_security_middleware


def _security_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def secure(request: Request, call_next):
        return await portal_security_middleware(request, call_next)

    @app.get("/")
    async def home(request: Request) -> HTMLResponse:
        return HTMLResponse(f'<meta name="trakt-csrf-token" content="{request.state.csrf_token}">')

    @app.post("/json")
    async def json_post() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/form")
    async def form_post(request: Request) -> JSONResponse:
        form = await request.form()
        return JSONResponse({"value": form.get("value")})

    return app


def test_csrf_cookie_and_security_headers_are_set() -> None:
    response = TestClient(_security_app(), base_url="http://127.0.0.1").get("/")

    assert response.status_code == 200
    assert "trakt_csrf=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


def test_csrf_accepts_header_and_form_tokens_and_replays_form_body() -> None:
    client = TestClient(_security_app(), base_url="http://127.0.0.1")
    client.get("/")
    token = client.cookies["trakt_csrf"]

    json_response = client.post("/json", headers={"X-Trakt-CSRF": token}, json={"value": 1})
    form_response = client.post("/form", data={"_csrf": token, "value": "saved"})
    multipart_response = client.post(
        "/form",
        data={"_csrf": token, "value": "uploaded"},
        files={"asset": ("sample.txt", b"payload", "text/plain")},
    )

    assert json_response.json() == {"ok": True}
    assert form_response.json() == {"value": "saved"}
    assert multipart_response.json() == {"value": "uploaded"}


def test_csrf_rejects_missing_mismatched_and_external_origin() -> None:
    client = TestClient(_security_app(), base_url="http://127.0.0.1")
    client.get("/")
    token = client.cookies["trakt_csrf"]

    assert client.post("/json", json={}).status_code == 403
    assert client.post("/json", headers={"X-Trakt-CSRF": "wrong"}, json={}).status_code == 403
    assert client.post(
        "/json",
        headers={"X-Trakt-CSRF": token, "Origin": "https://attacker.example"},
        json={},
    ).status_code == 403


def test_release_runtime_rejects_non_local_host(monkeypatch) -> None:
    monkeypatch.delenv("TRAKT_TRACKER_ALLOW_LAN", raising=False)
    response = TestClient(_security_app(), base_url="http://127.0.0.1").get("/", headers={"Host": "attacker.example"})
    assert response.status_code == 400


def test_health_is_available_before_setup_and_setup_contains_csrf_token(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(database_path=str(tmp_path / "legacy.sqlite3")))
    app = create_app(store)
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        health = client.get("/healthz")
        setup = client.get("/setup")
    finally:
        app.state.runtime.close()

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert set(health.json()) == {"status", "version"}
    assert 'name="trakt-csrf-token"' in setup.text
    assert client.cookies["trakt_csrf"] in setup.text
