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
            "ALTER TABLE episodes_cache ADD COLUMN imdb_id VARCHAR(32) DEFAULT ''",
            "ALTER TABLE episodes_cache ADD COLUMN imdb_rating FLOAT",
            "ALTER TABLE episodes_cache ADD COLUMN imdb_votes INTEGER",
            "ALTER TABLE episodes_cache ADD COLUMN trakt_rating FLOAT",
            "ALTER TABLE episodes_cache ADD COLUMN trakt_votes INTEGER",
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
