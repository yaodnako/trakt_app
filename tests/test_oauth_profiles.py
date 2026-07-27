from __future__ import annotations

import json
import http.client
import socket
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from time import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import trakt_tracker.application.services as services_module
from trakt_tracker.application.services import AuthService
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.infrastructure.keyring_store import TokenBundle
from trakt_tracker.infrastructure.trakt.client import (
    OAuthDeviceAuthorization,
    TraktClient,
    TraktError,
)
from trakt_tracker.infrastructure.trakt.oauth import (
    AuthorizationResult,
    OAuthCallbackServer,
    OAuthCallbackUnavailable,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _request(port: int, path: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def test_oauth_callback_success_and_exact_path() -> None:
    port = _free_port()
    server = OAuthCallbackServer(f"http://127.0.0.1:{port}/callback", expected_state="expected")
    server.start()

    assert _request(port, "/wrong?code=ignored&state=expected") == 404
    assert _request(port, "/callback?code=accepted&state=expected") == 200
    assert server.wait_for_code(timeout=1) == AuthorizationResult(code="accepted", state="expected")


def test_oauth_callback_rejects_wrong_state() -> None:
    port = _free_port()
    server = OAuthCallbackServer(f"http://127.0.0.1:{port}/callback", expected_state="expected")
    server.start()

    assert _request(port, "/callback?code=accepted&state=wrong") == 400
    with pytest.raises(RuntimeError, match="state mismatch"):
        server.wait_for_code(timeout=1)


def test_oauth_callback_reports_user_denial_immediately() -> None:
    port = _free_port()
    server = OAuthCallbackServer(f"http://127.0.0.1:{port}/callback", expected_state="expected")
    server.start()

    assert _request(port, "/callback?error=access_denied&state=expected") == 400
    with pytest.raises(RuntimeError, match="access_denied"):
        server.wait_for_code(timeout=1)


def test_oauth_callback_times_out() -> None:
    port = _free_port()
    server = OAuthCallbackServer(f"http://127.0.0.1:{port}/callback", expected_state="expected")

    with pytest.raises(TimeoutError):
        server.wait_for_code(timeout=0.05)


def test_oauth_callback_refuses_port_owned_by_another_service() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    blocker = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=blocker.serve_forever, daemon=True)
    thread.start()
    port = int(blocker.server_address[1])
    callback = OAuthCallbackServer(
        f"http://127.0.0.1:{port}/callback",
        expected_state="expected",
    )
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            callback.start()
    finally:
        callback.close()
        blocker.shutdown()
        blocker.server_close()
        thread.join(timeout=1)


class _TokenStore:
    def __init__(self) -> None:
        self.saved: dict[str, TokenBundle] = {}

    def load(self, slug: str):
        return self.saved.get(slug)

    def save(self, slug: str, bundle: TokenBundle) -> None:
        self.saved[slug] = bundle

    def delete(self, slug: str) -> None:
        self.saved.pop(slug, None)


class _OAuthTokens:
    def __init__(self, bundle: TokenBundle) -> None:
        self._bundle = bundle

    def to_bundle(self) -> TokenBundle:
        return self._bundle


def test_authorization_saves_token_under_returned_profile_and_uses_random_state(tmp_path: Path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(client_id="client", client_secret="secret"))
    token_store = _TokenStore()
    bundle = TokenBundle("access", "refresh", 1, 3600)
    opened_urls: list[str] = []
    states: list[str] = []

    class Callback:
        def __init__(self, _redirect_uri: str, *, expected_state: str) -> None:
            states.append(expected_state)

        def start(self) -> None:
            return None

        def wait_for_code(self):
            return AuthorizationResult(code="oauth-code", state=states[-1])

        def close(self) -> None:
            return None

    class Client:
        def exchange_code(self, code: str):
            assert code == "oauth-code"
            return _OAuthTokens(bundle)

        def set_tokens(self, saved: TokenBundle) -> None:
            assert saved == bundle

        def get_me(self):
            return {"user": {"ids": {"slug": "new-viewer"}}}

    monkeypatch.setattr(services_module, "OAuthCallbackServer", Callback)
    monkeypatch.setattr(services_module, "open_authorization_url", opened_urls.append)
    auth = AuthService(store, token_store, lambda _config: Client())

    assert auth.authorize() == "new-viewer"
    assert token_store.saved["new-viewer"] == bundle
    assert "new-viewer" in store.load().known_profile_slugs
    assert len(states[0]) >= 32
    assert parse_qs(urlparse(opened_urls[0]).query)["state"] == [states[0]]


def test_authorization_uses_device_flow_when_callback_port_is_occupied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(client_id="client", client_secret="secret"))
    token_store = _TokenStore()
    bundle = TokenBundle("access", "refresh", 1, 3600)
    opened_urls: list[str] = []
    authorization = OAuthDeviceAuthorization(
        device_code="device-code",
        user_code="ABCD1234",
        verification_url="https://trakt.tv/activate",
        expires_in=600,
        interval=5,
    )

    class Callback:
        def __init__(self, _redirect_uri: str, *, expected_state: str) -> None:
            return None

        def start(self) -> None:
            raise OAuthCallbackUnavailable("callback port already in use")

    class Client:
        def start_device_authorization(self) -> OAuthDeviceAuthorization:
            return authorization

        def wait_for_device_authorization(self, received: OAuthDeviceAuthorization):
            assert received is authorization
            return _OAuthTokens(bundle)

        def set_tokens(self, saved: TokenBundle) -> None:
            assert saved == bundle

        def get_me(self):
            return {"user": {"ids": {"slug": "new-viewer"}}}

    monkeypatch.setattr(services_module, "OAuthCallbackServer", Callback)
    monkeypatch.setattr(services_module, "open_authorization_url", opened_urls.append)
    auth = AuthService(store, token_store, lambda _config: Client())

    assert auth.authorize() == "new-viewer"
    assert opened_urls == ["https://trakt.tv/activate/ABCD1234"]
    assert token_store.saved["new-viewer"] == bundle


def test_parallel_authorization_attempt_joins_in_progress_attempt(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(client_id="client", client_secret="secret"))
    auth = AuthService(store, _TokenStore(), lambda _config: None)
    entered = Event()
    release = Event()
    second_started = Event()
    second_finished = Event()
    calls: list[str] = []
    results: list[str | None] = [None, None]
    errors: list[BaseException] = []

    def blocked_authorization() -> str:
        calls.append("authorize")
        entered.set()
        release.wait(timeout=2)
        return "viewer"

    def run_authorization(index: int, *, started: Event | None = None, finished: Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            results[index] = auth.authorize()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    auth._authorize_once = blocked_authorization
    first = Thread(target=run_authorization, args=(0,))
    first.start()
    assert entered.wait(timeout=1)
    second = Thread(
        target=run_authorization,
        args=(1,),
        kwargs={"started": second_started, "finished": second_finished},
    )
    second.start()
    assert second_started.wait(timeout=1)
    try:
        assert not second_finished.wait(timeout=0.1)
    finally:
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert results == ["viewer", "viewer"]
    assert calls == ["authorize"]


def test_disconnect_removes_only_selected_profile_token(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig())
    token_store = _TokenStore()
    token_store.saved["alpha"] = TokenBundle("alpha-access", "alpha-refresh", 1, 3600)
    token_store.saved["beta"] = TokenBundle("beta-access", "beta-refresh", 1, 3600)
    auth = AuthService(store, token_store, lambda _config: None)

    auth.disconnect("alpha")

    assert "alpha" not in token_store.saved
    assert token_store.saved["beta"].access_token == "beta-access"


def _client_with_transport(handler) -> TraktClient:
    client = TraktClient("client", "secret", "http://127.0.0.1/callback")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _oauth_payload(*, access: str = "new-access", refresh: str = "new-refresh") -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "created_at": int(time()),
        "expires_in": 604800,
        "token_type": "bearer",
        "scope": "public offline_access",
    }


def test_oauth_token_parser_ignores_unknown_response_fields() -> None:
    payload = {**_oauth_payload(), "provider_extension": "new-field"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["grant_type"] == "authorization_code"
        return httpx.Response(200, json=payload)

    client = _client_with_transport(handler)
    try:
        tokens = client.exchange_code("oauth-code")
    finally:
        client.close()

    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == "new-refresh"


def test_device_authorization_polls_until_trakt_returns_tokens() -> None:
    calls: list[tuple[str, dict]] = []
    sleeps: list[float] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path == "/oauth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-code",
                    "user_code": "ABCD1234",
                    "verification_url": "https://trakt.tv/activate",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        assert request.url.path == "/oauth/device/token"
        poll_count += 1
        if poll_count == 1:
            return httpx.Response(400, json={"error": "pending"})
        return httpx.Response(200, json=_oauth_payload())

    client = TraktClient(
        "client",
        "secret",
        "http://127.0.0.1/callback",
        rate_limit_sleep=sleeps.append,
    )
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        authorization = client.start_device_authorization()
        tokens = client.wait_for_device_authorization(authorization)
    finally:
        client.close()

    assert authorization.activation_url == "https://trakt.tv/activate/ABCD1234"
    assert tokens.access_token == "new-access"
    assert sleeps == [5.0, 5.0]
    assert calls == [
        ("/oauth/device/code", {"client_id": "client"}),
        (
            "/oauth/device/token",
            {"code": "device-code", "client_id": "client", "client_secret": "secret"},
        ),
        (
            "/oauth/device/token",
            {"code": "device-code", "client_id": "client", "client_secret": "secret"},
        ),
    ]


def test_expired_token_refreshes_before_authenticated_request() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json=_oauth_payload())
        assert request.headers["Authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(handler)
    client.set_tokens(TokenBundle("expired-access", "old-refresh", 1, 1))
    saved: list[TokenBundle] = []
    client.set_token_refresh_callback(saved.append)
    try:
        result = client._request("GET", "/protected", use_cache=False)
    finally:
        client.close()

    assert result == {"ok": True}
    assert calls == [("POST", "/oauth/token"), ("GET", "/protected")]
    assert [bundle.access_token for bundle in saved] == ["new-access"]


def test_401_refresh_keeps_new_token_when_first_persistence_attempt_fails() -> None:
    calls: list[tuple[str, str]] = []
    save_attempts: list[TokenBundle] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json=_oauth_payload())
        if request.headers["Authorization"] == "Bearer old-access":
            return httpx.Response(401, json={"error": "unauthorized"})
        assert request.headers["Authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"ok": True})

    def persist(bundle: TokenBundle) -> None:
        save_attempts.append(bundle)
        if len(save_attempts) == 1:
            raise RuntimeError("credential manager temporarily unavailable")

    client = _client_with_transport(handler)
    client.set_tokens(TokenBundle("old-access", "old-refresh", int(time()), 604800))
    client.set_token_refresh_callback(persist)
    try:
        result = client._request("GET", "/protected", use_cache=False)
    finally:
        client.close()

    assert result == {"ok": True}
    assert calls == [
        ("GET", "/protected"),
        ("POST", "/oauth/token"),
        ("GET", "/protected"),
    ]
    assert len(save_attempts) == 2
    assert client._token is not None
    assert client._token.access_token == "new-access"


def test_invalid_grant_requires_reconnect_and_is_not_retried() -> None:
    remote_calls = 0
    invalidated: list[bool] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal remote_calls
        remote_calls += 1
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "session not found"},
        )

    client = _client_with_transport(handler)
    client.set_tokens(TokenBundle("expired-access", "dead-refresh", 1, 1))
    client.set_reauthorization_callback(lambda: invalidated.append(True))
    try:
        with pytest.raises(TraktError, match="Reconnect required"):
            client._request("GET", "/protected", use_cache=False)
        with pytest.raises(TraktError, match="Reconnect required"):
            client._request("GET", "/protected", use_cache=False)
    finally:
        client.close()

    assert remote_calls == 1
    assert invalidated == [True]
    assert client._token is None


def test_parallel_401_responses_use_single_refresh_exchange() -> None:
    old_requests = Barrier(2)
    counter_lock = Lock()
    refresh_calls = 0
    saved: list[TokenBundle] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        if request.url.path == "/oauth/token":
            with counter_lock:
                refresh_calls += 1
            return httpx.Response(200, json=_oauth_payload())
        if request.headers["Authorization"] == "Bearer old-access":
            old_requests.wait(timeout=2)
            return httpx.Response(401, json={"error": "unauthorized"})
        assert request.headers["Authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(handler)
    client.set_tokens(TokenBundle("old-access", "old-refresh", int(time()), 604800))
    client.set_token_refresh_callback(saved.append)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: client._request("GET", "/protected", use_cache=False), range(2)))
    finally:
        client.close()

    assert results == [{"ok": True}, {"ok": True}]
    assert refresh_calls == 1
    assert len(saved) == 1


def test_auth_service_loads_profile_token_only_when_client_is_created(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(
        AppConfig(
            client_id="client",
            client_secret="secret",
            active_profile_slug="alpha",
            known_profile_slugs=["alpha"],
        )
    )
    token_store = _TokenStore()
    original = TokenBundle("old-access", "old-refresh", 1, 604800)
    replacement = TokenBundle("other-access", "other-refresh", 2, 604800)
    token_store.saved["alpha"] = original

    class Client:
        def __init__(self) -> None:
            self.loaded: list[TokenBundle | None] = []

        def set_tokens(self, bundle: TokenBundle | None) -> None:
            self.loaded.append(bundle)

        def set_token_refresh_callback(self, _callback) -> None:
            return None

        def set_reauthorization_callback(self, _callback) -> None:
            return None

    client = Client()
    auth = AuthService(store, token_store, lambda _config: client)

    assert auth.get_client() is client
    token_store.saved["alpha"] = replacement
    assert auth.get_client() is client
    assert client.loaded == [original]
