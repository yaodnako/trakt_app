from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


LATEST_SCHEMA_VERSION = 5


class Database:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._engine = create_engine(
            f"sqlite:///{self._path}",
            future=True,
            connect_args={"timeout": 15, "check_same_thread": False},
        )
        event.listen(self._engine, "connect", self._configure_sqlite_connection)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, class_=Session)

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    def create_schema(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._path.with_name(f".{self._path.name}.schema.lock")):
            existed_before_open = self._path.exists()
            if existed_before_open:
                self._quick_check()
            current_version = self._current_schema_version()
            if current_version < LATEST_SCHEMA_VERSION and existed_before_open:
                self._backup_before_migration()
            Base.metadata.create_all(self._engine)
            self._ensure_schema_migration_table()
            current_version = self._current_schema_version()
            # Additive migrations and derived-state repairs are intentionally idempotent.
            self._apply_migrations()
            if current_version < LATEST_SCHEMA_VERSION:
                with self._engine.begin() as conn:
                    conn.execute(
                        text("INSERT INTO schema_migrations (version, applied_at) VALUES (:version, :applied_at)"),
                        {"version": LATEST_SCHEMA_VERSION, "applied_at": datetime.now(tz=UTC).isoformat()},
                    )
            self._quick_check()

    def _ensure_schema_migration_table(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, "
                    "applied_at TEXT NOT NULL"
                    ")"
                )
            )

    def _current_schema_version(self) -> int:
        with self._engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'")
            ).scalar()
            if not exists:
                return 0
            version = conn.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")).scalar()
        return int(version or 0)

    def _quick_check(self) -> None:
        with self._engine.connect() as conn:
            result = str(conn.execute(text("PRAGMA quick_check")).scalar() or "")
        if result.casefold() != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {self._path.name}: {result}")

    def _backup_before_migration(self) -> None:
        backup_path = self._path.with_name(f"{self._path.name}.pre-migration.bak")
        temporary = backup_path.with_name(f".{backup_path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(str(self._path))
        destination = sqlite3.connect(str(temporary))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.replace(temporary, backup_path)

    def _apply_migrations(self) -> None:
        additions = {
            "titles": (
                "trakt_rating FLOAT", "trakt_votes INTEGER", "tmdb_id INTEGER", "tmdb_rating FLOAT", "tmdb_votes INTEGER",
                "imdb_id VARCHAR(32) DEFAULT ''", "imdb_rating FLOAT", "imdb_votes INTEGER",
                "poster_status VARCHAR(32) DEFAULT 'unknown'", "poster_refreshed_at DATETIME",
                "backdrop_url VARCHAR(512) DEFAULT ''", "backdrop_status VARCHAR(32) DEFAULT 'unknown'",
                "backdrop_refreshed_at DATETIME", "ratings_status VARCHAR(32) DEFAULT 'unknown'", "ratings_refreshed_at DATETIME",
            ),
            "episodes_cache": (
                "still_url VARCHAR(512) DEFAULT ''", "still_missing BOOLEAN DEFAULT 0", "imdb_id VARCHAR(32) DEFAULT ''",
                "imdb_rating FLOAT", "imdb_votes INTEGER", "trakt_rating FLOAT", "trakt_votes INTEGER",
                "still_status VARCHAR(32) DEFAULT 'unknown'", "still_refreshed_at DATETIME",
                "trakt_details_status VARCHAR(32) DEFAULT 'unknown'", "trakt_details_refreshed_at DATETIME",
                "imdb_match_status VARCHAR(32) DEFAULT 'unknown'", "imdb_match_attempt_key VARCHAR(64) DEFAULT ''",
                "imdb_season INTEGER", "imdb_episode INTEGER", "imdb_coordinates_revision VARCHAR(64) DEFAULT ''",
            ),
            "history_events": ("watched_at_known BOOLEAN DEFAULT 1",),
            "notifications_log": ("last_sent_at DATETIME", "seen_at DATETIME", "notify_count INTEGER DEFAULT 1"),
            "release_tracking_state": ("list_count INTEGER",),
            "user_title_state": ("paused BOOLEAN DEFAULT 0",),
        }
        with self._engine.begin() as conn:
            for table, definitions in additions.items():
                existing = {
                    str(row[1]).casefold()
                    for row in conn.execute(text(f'PRAGMA table_info("{table}")'))
                }
                for definition in definitions:
                    column = definition.split(None, 1)[0]
                    if column.casefold() not in existing:
                        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {definition}'))
            conn.execute(
                    text(
                        "UPDATE notifications_log "
                        "SET last_sent_at = COALESCE(last_sent_at, sent_at), "
                        "notify_count = COALESCE(notify_count, 1)"
                    )
            )
            conn.execute(
                    text(
                        "UPDATE titles "
                        "SET poster_status = CASE "
                        "WHEN COALESCE(poster_status, '') = '' AND COALESCE(poster_url, '') != '' THEN 'ready' "
                        "WHEN COALESCE(poster_status, '') = '' THEN 'unknown' "
                        "ELSE poster_status END, "
                        "poster_refreshed_at = CASE "
                        "WHEN poster_refreshed_at IS NOT NULL THEN poster_refreshed_at "
                        "WHEN COALESCE(poster_url, '') != '' OR COALESCE(poster_status, '') IN ('ready', 'checked_no_data', 'retryable_failure') THEN updated_at "
                        "ELSE poster_refreshed_at END, "
                        "backdrop_status = CASE "
                        "WHEN COALESCE(backdrop_status, '') = '' AND COALESCE(backdrop_url, '') != '' THEN 'ready' "
                        "WHEN COALESCE(backdrop_status, '') = '' THEN 'unknown' "
                        "ELSE backdrop_status END, "
                        "backdrop_refreshed_at = CASE "
                        "WHEN backdrop_refreshed_at IS NOT NULL THEN backdrop_refreshed_at "
                        "WHEN COALESCE(backdrop_url, '') != '' OR COALESCE(backdrop_status, '') IN ('ready', 'checked_no_data', 'retryable_failure') THEN updated_at "
                        "ELSE backdrop_refreshed_at END, "
                        "ratings_status = CASE "
                        "WHEN COALESCE(ratings_status, '') = '' AND trakt_rating IS NOT NULL AND trakt_votes IS NOT NULL THEN 'ready' "
                        "WHEN COALESCE(ratings_status, '') = '' THEN 'unknown' "
                        "ELSE ratings_status END"
                    )
            )
            conn.execute(
                    text(
                        "UPDATE episodes_cache "
                        "SET still_status = CASE "
                        "WHEN COALESCE(still_status, '') = '' AND COALESCE(still_url, '') != '' THEN 'ready' "
                        "WHEN COALESCE(still_status, '') = '' AND COALESCE(still_missing, 0) != 0 THEN 'checked_no_data' "
                        "WHEN COALESCE(still_status, '') = '' THEN 'unknown' "
                        "ELSE still_status END, "
                        "still_refreshed_at = CASE "
                        "WHEN still_refreshed_at IS NOT NULL THEN still_refreshed_at "
                        "WHEN COALESCE(still_url, '') != '' OR COALESCE(still_status, '') IN ('ready', 'checked_no_data', 'retryable_failure') OR COALESCE(still_missing, 0) != 0 THEN CURRENT_TIMESTAMP "
                        "ELSE still_refreshed_at END, "
                        "trakt_details_status = CASE "
                        "WHEN COALESCE(trakt_details_status, '') = '' AND trakt_rating IS NOT NULL AND trakt_votes IS NOT NULL THEN 'ready' "
                        "WHEN COALESCE(trakt_details_status, '') = '' THEN 'unknown' "
                        "ELSE trakt_details_status END, "
                        "imdb_match_status = CASE "
                        "WHEN COALESCE(imdb_id, '') != '' THEN 'resolved' "
                        "WHEN COALESCE(imdb_match_status, '') NOT IN ('unknown', 'no_match') THEN 'unknown' "
                        "ELSE COALESCE(NULLIF(imdb_match_status, ''), 'unknown') END, "
                        "imdb_match_attempt_key = COALESCE(imdb_match_attempt_key, ''), "
                        "imdb_coordinates_revision = COALESCE(imdb_coordinates_revision, '')"
                    )
            )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self._engine.dispose()


@contextmanager
def _exclusive_file_lock(path: Path):
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
