from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QPlainTextEdit, QSystemTrayIcon

from trakt_tracker.application.services import ServiceContainer, build_services
from trakt_tracker.config import ConfigStore
from trakt_tracker.persistence.database import Database
from trakt_tracker.ui.main_window import load_app_icon

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except ImportError:  # pragma: no cover - depends on the local PySide install
    QAudioOutput = None
    QMediaPlayer = None


WEB_URL = "http://127.0.0.1:8000"
NOTIFICATION_POLL_INTERVAL_MS = 60_000


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _build_tray_services() -> tuple[ConfigStore, ServiceContainer]:
    config_store = ConfigStore()
    config = config_store.load()
    db = Database(config.resolved_database_path)
    db.create_schema()
    return config_store, build_services(config_store, db)


class TrayNotificationPoller(QObject):
    notificationsReceived = Signal(list)
    logMessage = Signal(str)

    def __init__(self, services: ServiceContainer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._services = services
        self._timer = QTimer(self)
        self._timer.setInterval(NOTIFICATION_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.poll)
        self._lock = Lock()
        self._running = False

    def start(self) -> None:
        QTimer.singleShot(5000, self.poll)
        self._timer.start()

    def poll(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        Thread(target=self._run, name="tray-notification-poll", daemon=True).start()

    def _run(self) -> None:
        try:
            if not self._services.auth.is_authorized():
                return
            items = self._services.notifications.poll_upcoming(send_native=True)
            if not items:
                return
            try:
                self._services.progress.sync_progress(dropped_only=False)
            except Exception as exc:
                self.logMessage.emit(f"notification progress sync failed: {exc}")
            self.notificationsReceived.emit(items)
        except Exception as exc:
            self.logMessage.emit(f"notification poll failed: {exc}")
        finally:
            with self._lock:
                self._running = False


class WebPortalTrayWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Trakt Tracker Web Portal")
        self.resize(900, 520)
        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self.setCentralWidget(self._log)
        self._process = QProcess(self)
        self._process.setProgram(sys.executable)
        self._process.setArguments(["-m", "trakt_tracker.web.main"])
        self._process.setWorkingDirectory(str(_project_root()))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("TRAKT_TRACKER_TRAY_RUNTIME", "1")
        self._process.setProcessEnvironment(env)
        self._process.readyReadStandardOutput.connect(lambda: self._append_process_output(False))
        self._process.readyReadStandardError.connect(lambda: self._append_process_output(True))
        self._process.started.connect(lambda: self._append_log("web portal process started"))
        self._process.errorOccurred.connect(lambda error: self._append_log(f"web portal process error: {getattr(error, 'name', str(error))}"))
        self._process.finished.connect(
            lambda code, status: self._append_log(f"web portal process exited: code={code} status={getattr(status, 'name', str(status))}")
        )
        self._config_store: ConfigStore | None = None
        self._services: ServiceContainer | None = None
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
        self._append_log(f"starting web portal at {WEB_URL}")
        self._process.start()
        self._start_notification_poller()
        self.hide()

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()

    def _build_tray_menu(self) -> QMenu:
        menu = QMenu()
        show_log = QAction("Show log", self)
        show_log.triggered.connect(self._show_log_window)
        open_portal = QAction("Open portal", self)
        open_portal.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(WEB_URL)))
        restart = QAction("Restart web portal", self)
        restart.triggered.connect(self._restart_process)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(show_log)
        menu.addAction(open_portal)
        menu.addSeparator()
        menu.addAction(restart)
        menu.addSeparator()
        menu.addAction(quit_action)
        return menu

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_log_window()

    def _show_log_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _append_process_output(self, stderr: bool) -> None:
        data = self._process.readAllStandardError() if stderr else self._process.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace").strip()
        if text:
            self._append_log(text)

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{stamp}] {message}")

    def _start_notification_poller(self) -> None:
        try:
            self._config_store, self._services = _build_tray_services()
        except Exception as exc:
            self._append_log(f"notification poller disabled: {exc}")
            return
        self._notification_poller = TrayNotificationPoller(self._services, self)
        self._notification_poller.notificationsReceived.connect(self._on_notifications_received)
        self._notification_poller.logMessage.connect(self._append_log)
        self._notification_poller.start()
        self._append_log("tray notification poller started")

    def _on_notifications_received(self, items: list) -> None:
        self._append_log(f"notifications received: {len(items)}")
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

    def _restart_process(self) -> None:
        self._append_log("restarting web portal")
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(4000):
                self._process.kill()
                self._process.waitForFinished(2000)
        self._process.start()

    def _quit(self) -> None:
        self._append_log("stopping web portal")
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(4000):
                self._process.kill()
                self._process.waitForFinished(2000)
        self._tray.hide()
        QApplication.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(load_app_icon())
    window = WebPortalTrayWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
