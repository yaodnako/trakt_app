from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trakt_tracker.application.initial_setup import run_initial_setup
from trakt_tracker.persistence.database import Database
from trakt_tracker.profiles import read_setup_state, recover_interrupted_setup, write_setup_state


class _HistoryImport:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.defer_enrichment = False

    def initial_import(self, *, status_callback, defer_enrichment: bool = False) -> None:
        self.calls += 1
        self.defer_enrichment = defer_enrichment
        status_callback("History halfway complete.")
        if self.error is not None:
            raise self.error


class _ProgressImport:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.defer_assets = False

    def sync_progress(self, *, dropped_only: bool, force_full_assets: bool, defer_assets: bool = False) -> None:
        assert dropped_only is False
        assert force_full_assets is False
        self.calls += 1
        self.defer_assets = defer_assets
        if self.error is not None:
            raise self.error


def _services(tmp_path: Path, *, history_error=None, progress_error=None):
    database = Database(tmp_path / "profile.sqlite3")
    database.create_schema()
    history = _HistoryImport(error=history_error)
    progress = _ProgressImport(error=progress_error)
    return SimpleNamespace(database=database, sync=history, progress=progress), history, progress


def test_initial_setup_runs_history_then_progress_and_completes(tmp_path: Path) -> None:
    services, history, progress = _services(tmp_path)
    try:
        state = run_initial_setup(services)
    finally:
        services.database.close()

    assert history.calls == 1
    assert progress.calls == 1
    assert history.defer_enrichment is True
    assert progress.defer_assets is True
    assert state["state"] == "complete"
    assert state["completed_stages"] == ["history", "progress"]


def test_initial_setup_records_history_failure(tmp_path: Path) -> None:
    services, history, progress = _services(tmp_path, history_error=RuntimeError("history unavailable"))
    try:
        with pytest.raises(RuntimeError, match="history unavailable"):
            run_initial_setup(services)
        state = read_setup_state(services.database)
    finally:
        services.database.close()

    assert history.calls == 1
    assert progress.calls == 0
    assert state["state"] == "failed"
    assert state["stage"] == "history"
    assert state["completed_stages"] == []
    assert state["error"] == "history unavailable"


def test_retry_after_progress_failure_skips_completed_history(tmp_path: Path) -> None:
    services, history, progress = _services(tmp_path, progress_error=RuntimeError("progress unavailable"))
    try:
        with pytest.raises(RuntimeError, match="progress unavailable"):
            run_initial_setup(services)
        failed = read_setup_state(services.database)
        progress.error = None
        completed = run_initial_setup(services)
    finally:
        services.database.close()

    assert failed["stage"] == "progress"
    assert failed["completed_stages"] == ["history"]
    assert history.calls == 1
    assert progress.calls == 2
    assert completed["state"] == "complete"


def test_running_setup_is_recovered_as_interrupted_after_restart(tmp_path: Path) -> None:
    services, _, _ = _services(tmp_path)
    try:
        write_setup_state(
            services.database,
            {
                "state": "running",
                "stage": "progress",
                "completed_stages": ["history"],
                "message": "Synchronizing progress.",
            },
        )
        recovered = recover_interrupted_setup(services.database, task_running=False)
    finally:
        services.database.close()

    assert recovered["state"] == "failed"
    assert recovered["stage"] == "progress"
    assert recovered["completed_stages"] == ["history"]
    assert "interrupted" in recovered["error"].lower()
