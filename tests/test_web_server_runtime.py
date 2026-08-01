from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

from trakt_tracker.web.server import EmbeddedWebServer, bind_loopback_socket
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.web.runtime import ProfileOperationCoordinator


def test_loopback_socket_falls_back_when_preferred_port_is_occupied() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    occupied_port = int(blocker.getsockname()[1])
    try:
        server_socket, selected_port = bind_loopback_socket(occupied_port)
    finally:
        blocker.close()
    try:
        assert selected_port != occupied_port
        assert server_socket.getsockname()[0] == "127.0.0.1"
    finally:
        server_socket.close()


def test_loopback_socket_cannot_be_claimed_by_two_runtime_instances() -> None:
    first_socket, first_port = bind_loopback_socket(0)
    try:
        second_socket, second_port = bind_loopback_socket(first_port)
        try:
            assert second_port != first_port
        finally:
            second_socket.close()
    finally:
        first_socket.close()


def test_embedded_server_starts_stops_and_restarts(monkeypatch) -> None:
    class FakeServer:
        def __init__(self, _config) -> None:
            self.started = False
            self.should_exit = False
            self.force_exit = False

        def run(self, *, sockets) -> None:
            assert sockets and sockets[0].getsockname()[0] == "127.0.0.1"
            self.started = True
            while not self.should_exit and not self.force_exit:
                time.sleep(0.005)

    import uvicorn
    import trakt_tracker.web.app as web_app

    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(web_app, "create_app", lambda: object())

    embedded = EmbeddedWebServer(preferred_port=0)
    for _ in range(2):
        assert embedded.start().startswith("http://127.0.0.1:")
        deadline = time.monotonic() + 2
        while not embedded.is_ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert embedded.is_ready()
        assert embedded.stop(timeout=1)
        assert not embedded.is_running()


def test_profile_operation_coordinator_serializes_sync_workflows() -> None:
    coordinator = ProfileOperationCoordinator()
    operations = OperationLog()
    first_started = threading.Event()
    release_first = threading.Event()
    completed = threading.Event()
    order: list[str] = []

    def full_sync() -> None:
        order.append("full")
        first_started.set()
        release_first.wait(timeout=1)

    def repair_sync() -> None:
        order.append("repair")
        completed.set()

    assert coordinator.start("settings_full_sync", source="Full sync", operations=operations, fn=full_sync)
    assert first_started.wait(timeout=1)
    assert coordinator.start("settings_repair_sync", source="Repair sync", operations=operations, fn=repair_sync)
    assert coordinator.is_running("settings_repair_sync")
    assert not coordinator.is_active("settings_repair_sync")
    assert coordinator.is_queued("settings_repair_sync")
    assert order == ["full"]
    release_first.set()
    assert completed.wait(timeout=1)
    assert order == ["full", "repair"]


def test_profile_operation_coordinator_coalesces_running_task_to_latest_rerun() -> None:
    coordinator = ProfileOperationCoordinator()
    operations = OperationLog()
    first_started = threading.Event()
    release_first = threading.Event()
    rerun_completed = threading.Event()
    order: list[str] = []

    def first_run() -> None:
        order.append("first")
        first_started.set()
        release_first.wait(timeout=1)

    def superseded_run() -> None:
        order.append("superseded")

    def latest_run() -> None:
        order.append("latest")
        rerun_completed.set()

    try:
        assert coordinator.start_coalesced(
            "progress_refresh_after_watch_3",
            source="Progress refresh after watch",
            operations=operations,
            fn=first_run,
        )
        assert first_started.wait(timeout=1)
        assert coordinator.start_coalesced(
            "progress_refresh_after_watch_3",
            source="Progress refresh after watch",
            operations=operations,
            fn=superseded_run,
        )
        assert coordinator.start_coalesced(
            "progress_refresh_after_watch_3",
            source="Progress refresh after watch",
            operations=operations,
            fn=latest_run,
        )
        release_first.set()
        assert rerun_completed.wait(timeout=1)
        assert order == ["first", "latest"]
    finally:
        release_first.set()
        coordinator.close(timeout=1)
