from __future__ import annotations

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.profiles import read_setup_state, write_setup_state


def run_initial_setup(services: ServiceContainer) -> dict:
    state = read_setup_state(services.database)
    if state.get("state") == "complete":
        return state

    completed = set(state.get("completed_stages", []))
    stage = "history" if "history" not in completed else "progress"
    try:
        if stage == "history":
            _write_running(services, stage, completed, "Importing complete Trakt history and ratings.")

            def report_history(message: str) -> None:
                _write_running(services, "history", completed, message)

            services.sync.initial_import(status_callback=report_history, defer_enrichment=True)
            completed.add("history")

        stage = "progress"
        if stage not in completed:
            _write_running(services, stage, completed, "Synchronizing show progress.")
            services.progress.sync_progress(
                dropped_only=False,
                force_refresh=True,
                force_full_assets=False,
                defer_assets=True,
            )
            completed.add("progress")

        return write_setup_state(
            services.database,
            {
                "state": "complete",
                "stage": "done",
                "completed_stages": ["history", "progress"],
                "message": "Initial synchronization completed.",
                "error": "",
            },
        )
    except Exception as exc:
        write_setup_state(
            services.database,
            {
                "state": "failed",
                "stage": stage,
                "completed_stages": [item for item in ("history", "progress") if item in completed],
                "message": f"Initial synchronization failed during {stage}.",
                "error": str(exc),
            },
        )
        raise


def _write_running(
    services: ServiceContainer,
    stage: str,
    completed: set[str],
    message: str,
) -> None:
    write_setup_state(
        services.database,
        {
            "state": "running",
            "stage": stage,
            "completed_stages": [item for item in ("history", "progress") if item in completed],
            "message": message,
            "error": "",
        },
    )
