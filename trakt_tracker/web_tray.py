from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock, Thread

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QPlainTextEdit, QSystemTrayIcon

from trakt_tracker.config import ConfigStore, get_app_data_dir
from trakt_tracker.infrastructure.windows_autostart import set_web_tray_autostart
from trakt_tracker.web.runtime import PortalRuntime
from trakt_tracker.web.server import EmbeddedWebServer

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except ImportError:  # pragma: no cover - depends on the local PySide install
    QAudioOutput = None
    QMediaPlayer = None


NOTIFICATION_POLL_INTERVAL_MS = 60_000
SERVER_MONITOR_INTERVAL_MS = 100
INSTANCE_COMMAND_TIMEOUT_MS = 750


def load_app_icon() -> QIcon:
    return QIcon(str(Path(__file__).resolve().parent / "web" / "static" / "trakt_logo_bw.svg"))


def configure_runtime_logging() -> Path:
    log_path = get_app_data_dir() / "logs" / "runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(handler, "baseFilename", "") == str(log_path) for handler in root.handlers):
        handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    return log_path


def instance_server_name(data_dir: Path | None = None) -> str:
    root = (data_dir or get_app_data_dir()).resolve()
    digest = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:16]
    return f"TraktTracker-{digest}"


def send_instance_command(name: str, command: str, *, timeout_ms: int = INSTANCE_COMMAND_TIMEOUT_MS) -> bool:
    connection = QLocalSocket()
    connection.connectToServer(name)
    if not connection.waitForConnected(timeout_ms):
        return False
    connection.write(f"{command}\n".encode("utf-8"))
    connection.flush()
    connection.waitForBytesWritten(timeout_ms)
    connection.disconnectFromServer()
    return True


class TrayNotificationPoller(QObject):
    notificationsReceived = Signal(list)
    logMessage = Signal(str)

    def __init__(self, runtime: PortalRuntime, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._timer = QTimer(self)
        self._timer.setInterval(NOTIFICATION_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.poll)
        self._lock = Lock()
        self._running = False

    def start(self) -> None:
        QTimer.singleShot(5000, self.poll)
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        Thread(target=self._run, name="tray-notification-poll", daemon=True).start()

    def _run(self) -> None:
        try:
            if self._runtime.refresh_active_profile():
                self.logMessage.emit(f"tray notification profile switched to {self._runtime.active_slug}")
            services = self._runtime.services

            def refresh_notifications() -> None:
                active_services = self._runtime.services
                if not active_services.auth.is_authorized():
                    return
                items = active_services.notifications.poll_upcoming(send_native=True)
                if not items:
                    return
                self.notificationsReceived.emit(items)
                active_services.progress.sync_progress(dropped_only=False)

            coordinator = getattr(self._runtime, "background_tasks", None)
            if coordinator is None:  # Compatibility path for lightweight embedded-runtime tests.
                refresh_notifications()
            else:
                coordinator.start(
                    "tray_notification_poll",
                    source="Tray notification refresh",
                    operations=services.operations,
                    fn=refresh_notifications,
                )
        except Exception as exc:
            self.logMessage.emit(f"notification poll failed: {exc}")
        finally:
            with self._lock:
                self._running = False


class WebPortalTrayWindow(QMainWindow):
    def __init__(self, *, open_on_ready: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("Trakt Tracker Web Portal")
        self.resize(900, 520)
        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self.setCentralWidget(self._log)
        self._config_store = ConfigStore()
        self._runtime = PortalRuntime(self._config_store)
        self._web_server = EmbeddedWebServer(app_factory=self._create_web_app)
        self._web_url = ""
        self._server_ready = False
        self._server_failure_reported = False
        self._open_when_ready = open_on_ready
        self._quitting = False
        self._notification_poller: TrayNotificationPoller | None = None
        self._audio_output = QAudioOutput(self) if QAudioOutput is not None else None
        self._audio_player = QMediaPlayer(self) if QMediaPlayer is not None else None
        if self._audio_player is not None and self._audio_output is not None:
            self._audio_player.setAudioOutput(self._audio_output)
            self._audio_output.setVolume(0.8)

        icon = load_app_icon()
        self.setWindowIcon(icon)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Trakt Tracker Web Portal")
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.setContextMenu(self._build_tray_menu())
        self._tray.show()

        self._server_monitor = QTimer(self)
        self._server_monitor.setInterval(SERVER_MONITOR_INTERVAL_MS)
        self._server_monitor.timeout.connect(self._monitor_web_server)
        self._server_monitor.start()
        self._start_web_server()
        self._start_notification_poller()
        self.hide()

    @property
    def web_url(self) -> str:
        return self._web_url

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()

    def _build_tray_menu(self) -> QMenu:
        menu = QMenu()
        open_portal = QAction("Open portal", self)
        open_portal.triggered.connect(self.open_portal)
        show_log = QAction("Show log", self)
        show_log.triggered.connect(self._show_log_window)
        restart = QAction("Restart web portal", self)
        restart.triggered.connect(self._restart_server)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(open_portal)
        menu.addAction(show_log)
        menu.addSeparator()
        menu.addAction(restart)
        menu.addSeparator()
        menu.addAction(quit_action)
        return menu

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_portal()

    def open_portal(self) -> None:
        if self._server_ready and self._web_url:
            QDesktopServices.openUrl(QUrl(self._web_url))
        else:
            self._open_when_ready = True
            self._append_log("portal will open when the web server is ready")

    def _show_log_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{stamp}] {message}")
        logging.getLogger("trakt_tracker.runtime").info(message)

    def _start_web_server(self) -> None:
        self._server_ready = False
        self._server_failure_reported = False
        try:
            self._web_url = self._web_server.start()
        except Exception as exc:
            self._append_log(f"web portal failed to start: {type(exc).__name__}: {exc}")
            self._server_failure_reported = True
            return
        self._append_log(f"starting web portal at {self._web_url}")

    def _monitor_web_server(self) -> None:
        if self._web_server.is_ready():
            if self._server_ready:
                return
            self._server_ready = True
            self._append_log(f"web portal ready at {self._web_url}")
            if self._open_when_ready:
                self._open_when_ready = False
                QDesktopServices.openUrl(QUrl(self._web_url))
            return
        if self._web_server.is_running() or self._server_failure_reported or self._quitting:
            return
        failure = self._web_server.failure or "server stopped unexpectedly"
        self._append_log(f"web portal stopped: {failure}")
        self._server_failure_reported = True
        self._server_ready = False

    def _start_notification_poller(self) -> None:
        self._notification_poller = TrayNotificationPoller(self._runtime, self)
        self._notification_poller.notificationsReceived.connect(self._on_notifications_received)
        self._notification_poller.logMessage.connect(self._append_log)
        self._notification_poller.start()
        self._append_log("tray notification poller started")

    def _create_web_app(self):
        from trakt_tracker.web.app import create_app

        return create_app(runtime=self._runtime)

    def _on_notifications_received(self, items: list) -> None:
        self._append_log(f"notifications received: {len(items)}")
        self._runtime.services.notifications.record_activity(items)
        self._play_notification_sound()

    def _play_notification_sound(self) -> None:
        sound_path = ""
        if self._config_store is not None:
            sound_path = str(self._config_store.load().notification_sound_path or "").strip()
        path = Path(sound_path).expanduser() if sound_path else None
        if path is not None and path.exists() and path.is_file() and self._audio_player is not None:
            self._audio_player.setSource(QUrl.fromLocalFile(str(path)))
            self._audio_player.play()
            return
        QApplication.beep()

    def _restart_server(self) -> None:
        self._append_log("restarting web portal")
        if not self._web_server.stop():
            self._append_log("web portal did not stop cleanly; restart cancelled")
            return
        self._start_web_server()

    def quit_application(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._append_log("stopping web portal")
        self._server_monitor.stop()
        if self._notification_poller is not None:
            self._notification_poller.stop()
        if not self._web_server.stop():
            self._append_log("web portal thread did not stop before shutdown")
        self._tray.hide()
        if self._runtime is not None:
            self._runtime.close()
        QApplication.quit()


class InstanceCommandServer(QObject):
    def __init__(self, server: QLocalServer, window: WebPortalTrayWindow) -> None:
        super().__init__(window)
        self._server = server
        self._window = window
        self._clients: set[QLocalSocket] = set()
        self._buffers: dict[QLocalSocket, str] = {}
        self._server.newConnection.connect(self._accept_connections)

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                continue
            self._clients.add(connection)
            self._buffers[connection] = ""
            connection.readyRead.connect(lambda item=connection: self._read_command(item))
            connection.disconnected.connect(lambda item=connection: self._discard(item))
            if connection.bytesAvailable():
                self._read_command(connection)

    def _read_command(self, connection: QLocalSocket) -> None:
        payload = self._buffers.get(connection, "") + bytes(connection.readAll()).decode("utf-8", errors="replace")
        commands = payload.split("\n")
        self._buffers[connection] = commands.pop()
        for command in commands:
            command = command.rstrip("\r")
            if command == "open":
                self._window.open_portal()
            elif command == "quit":
                self._window.quit_application()

    def _discard(self, connection: QLocalSocket) -> None:
        self._clients.discard(connection)
        self._buffers.pop(connection, None)
        connection.deleteLater()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Trakt Tracker in the Windows system tray.")
    parser.add_argument("--autostart", action="store_true", help="Start without opening the browser.")
    parser.add_argument("--quit", action="store_true", help="Stop an existing Trakt Tracker instance.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    os.environ["TRAKT_TRACKER_TRAY_RUNTIME"] = "1"
    configure_runtime_logging()
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(load_app_icon())

    name = instance_server_name()
    command = "quit" if args.quit else ("noop" if args.autostart else "open")
    if send_instance_command(name, command):
        return 0
    if args.quit:
        return 0

    command_server = QLocalServer()
    QLocalServer.removeServer(name)
    if not command_server.listen(name):
        if send_instance_command(name, command):
            return 0
        logging.getLogger("trakt_tracker.runtime").error("could not acquire the single-instance endpoint")
        return 1

    try:
        config = ConfigStore().load()
        if config.web_portal_start_with_windows:
            set_web_tray_autostart(True)
    except Exception as exc:
        logging.getLogger("trakt_tracker.runtime").warning("autostart registration repair failed: %s", exc)

    window = WebPortalTrayWindow(open_on_ready=not args.autostart)
    ipc_owner = InstanceCommandServer(command_server, window)
    exit_code = app.exec()
    del ipc_owner
    command_server.close()
    QLocalServer.removeServer(name)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
