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
    def __init__(self, redirect_uri: str, *, expected_state: str) -> None:
        parsed = urlparse(redirect_uri)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 8765
        self._path = parsed.path or "/"
        self._expected_state = expected_state
        self._result: AuthorizationResult | None = None
        self._error: str = ""
        self._event = threading.Event()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != parent._path:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                code = query.get("code", [""])[0]
                state = query.get("state", [""])[0]
                error = query.get("error", [""])[0]
                if state != parent._expected_state:
                    parent._error = "OAuth state mismatch"
                    parent._event.set()
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization failed. OAuth state did not match.")
                elif error:
                    parent._error = f"Trakt authorization was denied: {error}"
                    parent._event.set()
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization was not completed. You can close this window.")
                elif code:
                    parent._result = AuthorizationResult(code=code, state=state)
                    parent._event.set()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Authorization complete. You can close this window.")
                else:
                    parent._error = "Authorization code missing"
                    parent._event.set()
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization code missing.")

            def log_message(self, *_args) -> None:
                return

        self._server = HTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self._thread.start()

    def wait_for_code(self, timeout: int = 180) -> AuthorizationResult:
        self.start()
        if not self._event.wait(timeout):
            self.close()
            raise TimeoutError("Timed out waiting for Trakt authorization callback")
        self.close()
        if self._error:
            raise RuntimeError(self._error)
        if self._result is None:
            raise RuntimeError("Authorization callback completed without code")
        return self._result

    def close(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)


def build_authorization_url(client_id: str, redirect_uri: str, *, state: str) -> str:
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
