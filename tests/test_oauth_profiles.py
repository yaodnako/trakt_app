from __future__ import annotations

import http.client
import socket
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

import pytest

import trakt_tracker.application.services as services_module
from trakt_tracker.application.services import AuthService
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.infrastructure.keyring_store import TokenBundle
from trakt_tracker.infrastructure.trakt.oauth import AuthorizationResult, OAuthCallbackServer


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


def test_parallel_authorization_attempt_is_rejected(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(client_id="client", client_secret="secret"))
    auth = AuthService(store, _TokenStore(), lambda _config: None)
    entered = Event()
    release = Event()

    def blocked_authorization() -> str:
        entered.set()
        release.wait(timeout=2)
        return "viewer"

    auth._authorize_once = blocked_authorization
    worker = Thread(target=auth.authorize)
    worker.start()
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            auth.authorize()
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


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
