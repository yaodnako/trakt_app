from __future__ import annotations

import os
import sys
from time import perf_counter
from datetime import datetime, UTC

from PySide6.QtWidgets import QApplication

from trakt_tracker.application.services import build_services
from trakt_tracker.config import ConfigStore
from trakt_tracker.persistence.database import Database
from trakt_tracker.startup_profile import StartupProfiler
from trakt_tracker.ui.main_window import MainWindow, load_app_icon


def main() -> int:
    start = perf_counter()
    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())
    config_store = ConfigStore()
    config = config_store.load()
    profiler = StartupProfiler(config.resolved_database_path.parent / "startup.log", start_time=start)
    launch_epoch_ms = os.environ.get("TRAKT_LAUNCH_EPOCH_MS", "").strip()
    if launch_epoch_ms:
        try:
            launched_at = datetime.fromtimestamp(int(launch_epoch_ms) / 1000, tz=UTC)
            now_utc = datetime.now(tz=UTC)
            profiler.set_external_prefix("launcher -> python main", (now_utc - launched_at).total_seconds() * 1000)
        except (ValueError, OSError):
            pass
    profiler.mark("QApplication created")
    db = Database(config.resolved_database_path)
    profiler.mark("config loaded")
    db.create_schema()
    profiler.mark("database schema ready")
    services = build_services(config_store, db)
    profiler.mark("services built")
    window = MainWindow(services, startup_profiler=profiler)
    profiler.mark("MainWindow initialized")
    window.show()
    profiler.mark("window.show called")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
