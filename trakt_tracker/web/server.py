from __future__ import annotations

import os
import socket
from threading import Lock, Thread
from typing import Callable

from fastapi import FastAPI


def bind_loopback_socket(preferred_port: int = 8000) -> tuple[socket.socket, int]:
    last_error: OSError | None = None
    for port in (preferred_port, 0):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(("127.0.0.1", port))
            server_socket.set_inheritable(True)
            return server_socket, int(server_socket.getsockname()[1])
        except OSError as exc:
            last_error = exc
            server_socket.close()
    assert last_error is not None
    raise last_error


class EmbeddedWebServer:
    def __init__(self, *, preferred_port: int = 8000, app_factory: Callable[[], FastAPI] | None = None) -> None:
        self._preferred_port = preferred_port
        self._app_factory = app_factory
        self._thread: Thread | None = None
        self._server = None
        self._socket: socket.socket | None = None
        self._port = 0
        self._failure = ""
        self._stopping = False
        self._lock = Lock()

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}" if self._port else ""

    @property
    def failure(self) -> str:
        with self._lock:
            return self._failure

    def start(self) -> str:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.url
            self._failure = ""
            self._stopping = False
            self._socket, self._port = bind_loopback_socket(self._preferred_port)
            self._thread = Thread(target=self._run, name="trakt-web-server", daemon=True)
            self._thread.start()
            return self.url

    def _run(self) -> None:
        try:
            import uvicorn

            from trakt_tracker.web.app import create_app

            application = self._app_factory() if self._app_factory is not None else create_app()
            server = uvicorn.Server(
                uvicorn.Config(
                    application,
                    host="127.0.0.1",
                    port=self._port,
                    log_config=None,
                    access_log=False,
                )
            )
            with self._lock:
                self._server = server
                if self._stopping:
                    server.should_exit = True
            server.run(sockets=[self._socket] if self._socket is not None else None)
        except Exception as exc:
            with self._lock:
                self._failure = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._server = None
            self._close_socket()

    def is_ready(self) -> bool:
        with self._lock:
            server = self._server
        return bool(server is not None and server.started and self.is_running())

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def stop(self, timeout: float = 10.0) -> bool:
        with self._lock:
            self._stopping = True
            server = self._server
            thread = self._thread
            if server is not None:
                server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        stopped = thread is None or not thread.is_alive()
        if not stopped and thread is not None:
            with self._lock:
                if self._server is not None:
                    self._server.force_exit = True
            thread.join(timeout=2.0)
            stopped = not thread.is_alive()
        if stopped:
            self._close_socket()
        return stopped

    def _close_socket(self) -> None:
        with self._lock:
            server_socket = self._socket
            self._socket = None
        if server_socket is not None:
            try:
                server_socket.close()
            except OSError:
                pass
