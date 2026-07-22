from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from trakt_tracker.config import (
    ConfigStore,
    legacy_database_path,
    normalize_profile_slugs,
    profile_database_path,
)
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import SyncStateRepository


INITIAL_SETUP_KEY = "portal_initial_setup_v1"
PROFILE_MIGRATION_LOCK = ".profile-migration.lock"


@dataclass(frozen=True, slots=True)
class ProfileMigrationResult:
    slug: str
    database_path: Path
    copied_legacy_database: bool = False


def default_setup_state() -> dict:
    return {
        "state": "pending",
        "stage": "history",
        "completed_stages": [],
        "message": "Ready to sync Trakt data.",
        "error": "",
        "updated_at": "",
    }


def read_setup_state(db: Database) -> dict:
    repository = SyncStateRepository()
    with db.session() as session:
        raw = repository.get_value(session, INITIAL_SETUP_KEY, "")
    if not raw:
        return default_setup_state()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return default_setup_state()
    state = default_setup_state()
    if isinstance(payload, dict):
        state.update(payload)
    completed_stages = state.get("completed_stages", [])
    if not isinstance(completed_stages, list):
        completed_stages = []
    state["completed_stages"] = [
        item for item in completed_stages if item in {"history", "progress"}
    ]
    return state


def write_setup_state(db: Database, state: dict) -> dict:
    payload = default_setup_state()
    payload.update(state)
    payload["updated_at"] = datetime.now(tz=UTC).isoformat()
    repository = SyncStateRepository()
    with db.session() as session:
        repository.set_value(session, INITIAL_SETUP_KEY, json.dumps(payload, ensure_ascii=False))
    return payload


def mark_setup_complete(db: Database, message: str = "Initial setup completed.") -> dict:
    return write_setup_state(
        db,
        {
            "state": "complete",
            "stage": "done",
            "completed_stages": ["history", "progress"],
            "message": message,
            "error": "",
        },
    )


def recover_interrupted_setup(db: Database, *, task_running: bool) -> dict:
    state = read_setup_state(db)
    if state.get("state") == "running" and not task_running:
        state = write_setup_state(
            db,
            {
                **state,
                "state": "failed",
                "message": "Initial sync was interrupted. Retry to continue.",
                "error": "Initial sync was interrupted.",
            },
        )
    return state


def prepare_active_profile(config_store: ConfigStore) -> ProfileMigrationResult:
    config = config_store.load()
    slug = config.active_slug
    if not slug:
        return ProfileMigrationResult(slug="", database_path=config.resolved_database_path)
    destination = profile_database_path(config, slug)
    copied = False
    lock_path = destination.parent.parent / PROFILE_MIGRATION_LOCK
    with _exclusive_file_lock(lock_path):
        config = config_store.load()
        slug = config.active_slug
        destination = profile_database_path(config, slug)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = legacy_database_path(config)
        if not config.legacy_profile_migrated_slug:
            if not destination.exists() and source.exists() and source.resolve() != destination.resolve():
                _copy_sqlite_database(source, destination)
                copied = True
            elif destination.exists() and source.exists():
                copied = True
            config.legacy_profile_migrated_slug = slug
        config.known_profile_slugs = normalize_profile_slugs([*config.known_profile_slugs, slug])
        config.active_profile_slug = slug
        config.last_user_slug = slug
        config_store.save(config)
    return ProfileMigrationResult(slug=slug, database_path=destination, copied_legacy_database=copied)


def register_active_profile(config_store: ConfigStore, slug: str) -> ProfileMigrationResult:
    normalized = str(slug or "").strip()
    if not normalized:
        raise ValueError("Profile slug is required")
    config = config_store.load()
    config.active_profile_slug = normalized
    config.last_user_slug = normalized
    config.known_profile_slugs = normalize_profile_slugs([*config.known_profile_slugs, normalized])
    config_store.save(config)
    return prepare_active_profile(config_store)


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.migrating")
    if temporary.exists():
        temporary.unlink()
    source_connection = sqlite3.connect(str(source))
    destination_connection = sqlite3.connect(str(temporary))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.replace(temporary, destination)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - the product runtime is Windows-first
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - the product runtime is Windows-first
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
