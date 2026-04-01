from __future__ import annotations

import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse


AUTH_BASE_URL = "https://trakt.tv/oauth/authorize"


@dataclass(slots=True)
class AuthorizationResult:
    code: str
    state: str | None = None


class OAuthCallbackServer:
    def __init__(self, redirect_uri: str) -> None:
        parsed = urlparse(redirect_uri)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 8765
        self._result: AuthorizationResult | None = None
        self._event = threading.Event()

    def wait_for_code(self, timeout: int = 180) -> AuthorizationResult:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                code = query.get("code", [""])[0]
                state = query.get("state", [None])[0]
                if code:
                    parent._result = AuthorizationResult(code=code, state=state)
                    parent._event.set()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Authorization complete. You can close this window.")
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization code missing.")

            def log_message(self, *_args) -> None:
                return

        server = HTTPServer((self._host, self._port), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        if not self._event.wait(timeout):
            server.server_close()
            raise TimeoutError("Timed out waiting for Trakt authorization callback")
        server.server_close()
        if self._result is None:
            raise RuntimeError("Authorization callback completed without code")
        return self._result


def build_authorization_url(client_id: str, redirect_uri: str, state: str = "desktop-login") -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTH_BASE_URL}?{query}"


def open_authorization_url(url: str) -> None:
    webbrowser.open(url, new=1, autoraise=True)
