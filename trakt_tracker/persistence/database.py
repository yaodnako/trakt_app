from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, path: Path) -> None:
        self._engine = create_engine(
            f"sqlite:///{path}",
            future=True,
            connect_args={"timeout": 15, "check_same_thread": False},
        )
        event.listen(self._engine, "connect", self._configure_sqlite_connection)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, class_=Session)

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        statements = [
            "ALTER TABLE titles ADD COLUMN trakt_rating FLOAT",
            "ALTER TABLE titles ADD COLUMN trakt_votes INTEGER",
            "ALTER TABLE titles ADD COLUMN tmdb_id INTEGER",
            "ALTER TABLE titles ADD COLUMN tmdb_rating FLOAT",
            "ALTER TABLE titles ADD COLUMN tmdb_votes INTEGER",
            "ALTER TABLE titles ADD COLUMN imdb_id VARCHAR(32) DEFAULT ''",
            "ALTER TABLE titles ADD COLUMN imdb_rating FLOAT",
            "ALTER TABLE titles ADD COLUMN imdb_votes INTEGER",
            "ALTER TABLE titles ADD COLUMN poster_status VARCHAR(32) DEFAULT 'unknown'",
            "ALTER TABLE titles ADD COLUMN poster_refreshed_at DATETIME",
            "ALTER TABLE titles ADD COLUMN backdrop_url VARCHAR(512) DEFAULT ''",
            "ALTER TABLE titles ADD COLUMN backdrop_status VARCHAR(32) DEFAULT 'unknown'",
            "ALTER TABLE titles ADD COLUMN backdrop_refreshed_at DATETIME",
            "ALTER TABLE titles ADD COLUMN ratings_status VARCHAR(32) DEFAULT 'unknown'",
            "ALTER TABLE titles ADD COLUMN ratings_refreshed_at DATETIME",
            "ALTER TABLE episodes_cache ADD COLUMN still_url VARCHAR(512) DEFAULT ''",
            "ALTER TABLE episodes_cache ADD COLUMN still_missing BOOLEAN DEFAULT 0",
            "ALTER TABLE episodes_cache ADD COLUMN imdb_id VARCHAR(32) DEFAULT ''",
            "ALTER TABLE episodes_cache ADD COLUMN imdb_rating FLOAT",
            "ALTER TABLE episodes_cache ADD COLUMN imdb_votes INTEGER",
            "ALTER TABLE episodes_cache ADD COLUMN trakt_rating FLOAT",
            "ALTER TABLE episodes_cache ADD COLUMN trakt_votes INTEGER",
            "ALTER TABLE episodes_cache ADD COLUMN still_status VARCHAR(32) DEFAULT 'unknown'",
            "ALTER TABLE episodes_cache ADD COLUMN still_refreshed_at DATETIME",
            "ALTER TABLE episodes_cache ADD COLUMN trakt_details_status VARCHAR(32) DEFAULT 'unknown'",
            "ALTER TABLE episodes_cache ADD COLUMN trakt_details_refreshed_at DATETIME",
            "ALTER TABLE notifications_log ADD COLUMN last_sent_at DATETIME",
            "ALTER TABLE notifications_log ADD COLUMN seen_at DATETIME",
            "ALTER TABLE notifications_log ADD COLUMN notify_count INTEGER DEFAULT 1",
        ]
        with self._engine.begin() as conn:
            for statement in statements:
                try:
                    conn.execute(text(statement))
                except Exception:
                    continue
            try:
                conn.execute(
                    text(
                        "UPDATE notifications_log "
                        "SET last_sent_at = COALESCE(last_sent_at, sent_at), "
                        "notify_count = COALESCE(notify_count, 1)"
                    )
                )
            except Exception:
                pass
            try:
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
            except Exception:
                pass
            try:
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
                        "ELSE trakt_details_status END"
                    )
                )
            except Exception:
                pass

    @contextmanager
    def session(self) -> Session:
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
