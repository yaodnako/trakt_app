from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    CatalogIdentityMap,
    TmdbPreviewIntent,
    TmdbPreviewReleaseState,
    TmdbPreviewSnapshot,
)


class TmdbPreviewRepository:
    """SQLite persistence for the reversible TMDb catalog preview.

    The repository deliberately contains no network calls.  A route/service can
    update the projection and its desired intent in one ``Database.session``
    transaction, then wake the Trakt outbox after the commit.
    """

    def snapshot(self, session: Session, title_type: str, tmdb_id: int) -> TmdbPreviewSnapshot | None:
        return session.scalar(
            select(TmdbPreviewSnapshot).where(
                TmdbPreviewSnapshot.title_type == title_type,
                TmdbPreviewSnapshot.tmdb_id == int(tmdb_id),
            )
        )

    def upsert_snapshot(self, session: Session, payload: dict[str, Any]) -> TmdbPreviewSnapshot:
        title_type = str(payload.get("title_type") or "show")
        tmdb_id = int(payload.get("tmdb_id") or 0)
        row = self.snapshot(session, title_type, tmdb_id)
        if row is None:
            row = TmdbPreviewSnapshot(title_type=title_type, tmdb_id=tmdb_id)
            session.add(row)
        row.trakt_id = _optional_int(payload.get("trakt_id"))
        row.imdb_id = str(payload.get("imdb_id") or "")
        row.title = str(payload.get("title") or "")
        row.year = _optional_int(payload.get("year"))
        row.overview = str(payload.get("overview") or "")
        row.status = str(payload.get("status") or "")
        row.poster_url = str(payload.get("poster_url") or "")
        row.backdrop_url = str(payload.get("backdrop_url") or "")
        row.tmdb_rating = _optional_float(payload.get("tmdb_rating"))
        row.tmdb_votes = _optional_int(payload.get("tmdb_votes"))
        row.popularity = _optional_float(payload.get("popularity"))
        row.released_at = _parse_datetime(payload.get("released_at"))
        row.payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        session.flush()
        return row

    def identity(self, session: Session, title_type: str, tmdb_id: int) -> CatalogIdentityMap | None:
        return session.scalar(
            select(CatalogIdentityMap).where(
                CatalogIdentityMap.provider == "tmdb",
                CatalogIdentityMap.title_type == title_type,
                CatalogIdentityMap.provider_id == int(tmdb_id),
            )
        )

    def set_identity(
        self,
        session: Session,
        *,
        title_type: str,
        tmdb_id: int,
        trakt_id: int | None,
        imdb_id: str = "",
        status: str = "resolved",
    ) -> CatalogIdentityMap:
        row = self.identity(session, title_type, tmdb_id)
        if row is None:
            row = CatalogIdentityMap(
                provider="tmdb",
                title_type=title_type,
                provider_id=int(tmdb_id),
            )
            session.add(row)
        row.trakt_id = trakt_id
        row.imdb_id = str(imdb_id or "")
        row.status = str(status or "unknown")
        row.last_checked_at = datetime.now(tz=UTC).replace(tzinfo=None)
        session.flush()
        return row

    def intent(
        self,
        session: Session,
        *,
        operation_type: str,
        title_type: str,
        tmdb_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> TmdbPreviewIntent | None:
        return session.scalar(
            select(TmdbPreviewIntent).where(
                TmdbPreviewIntent.operation_type == operation_type,
                TmdbPreviewIntent.title_type == title_type,
                TmdbPreviewIntent.tmdb_id == int(tmdb_id),
                _nullable_equals(TmdbPreviewIntent.season, season),
                _nullable_equals(TmdbPreviewIntent.episode, episode),
            )
        )

    def effective_state(
        self,
        session: Session,
        *,
        operation_type: str,
        title_type: str,
        tmdb_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> bool:
        row = self.intent(
            session,
            operation_type=operation_type,
            title_type=title_type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
        )
        if row is None:
            return False
        return bool(_json_value(row.desired_state_json, False))

    def set_intent(
        self,
        session: Session,
        *,
        operation_type: str,
        title_type: str,
        tmdb_id: int,
        desired: bool,
        payload: dict[str, Any] | None = None,
        season: int | None = None,
        episode: int | None = None,
        mapped_trakt_id: int | None = None,
    ) -> TmdbPreviewIntent | None:
        payload = dict(payload or {})
        row = self.intent(
            session,
            operation_type=operation_type,
            title_type=title_type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
        )
        if row is None:
            base = False
            if not desired:
                return None
            row = TmdbPreviewIntent(
                operation_type=operation_type,
                title_type=title_type,
                tmdb_id=int(tmdb_id),
                season=season,
                episode=episode,
                base_state_json=json.dumps(base),
                desired_state_json=json.dumps(bool(desired)),
                payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                revision=1,
                status="mapped_pending" if mapped_trakt_id else "local_only",
                mapped_trakt_id=mapped_trakt_id,
            )
            session.add(row)
            session.flush()
            return row

        base = bool(_json_value(row.base_state_json, False))
        if not desired and desired == base and row.status == "local_only":
            session.delete(row)
            session.flush()
            return None
        row.desired_state_json = json.dumps(bool(desired))
        row.payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        row.revision = max(1, int(row.revision or 1) + 1)
        if row.status == "exported":
            row.status = "mapped_pending" if (mapped_trakt_id or row.mapped_trakt_id) else "local_only"
        if mapped_trakt_id is not None:
            row.mapped_trakt_id = int(mapped_trakt_id)
            if row.status == "local_only":
                row.status = "mapped_pending"
        session.flush()
        return row

    def list_intents(self, session: Session, *, status: str | None = None) -> list[TmdbPreviewIntent]:
        statement = select(TmdbPreviewIntent).order_by(TmdbPreviewIntent.updated_at, TmdbPreviewIntent.id)
        if status:
            statement = statement.where(TmdbPreviewIntent.status == status)
        return list(session.scalars(statement))

    def attach_mapping(self, session: Session, *, title_type: str, tmdb_id: int, trakt_id: int) -> int:
        rows = list(
            session.scalars(
                select(TmdbPreviewIntent).where(
                    TmdbPreviewIntent.title_type == title_type,
                    TmdbPreviewIntent.tmdb_id == int(tmdb_id),
                    TmdbPreviewIntent.status != "exported",
                )
            )
        )
        changed = 0
        for row in rows:
            row.mapped_trakt_id = int(trakt_id)
            if row.status == "local_only":
                row.status = "mapped_pending"
            changed += 1
        session.flush()
        return changed

    def mark_intent_exported(self, session: Session, item_id: int, revision: int) -> bool:
        row = session.get(TmdbPreviewIntent, int(item_id))
        if row is None or int(row.revision) != int(revision):
            return False
        row.status = "exported"
        session.flush()
        return True

    def delete_intent_if_revision(self, session: Session, item_id: int, revision: int) -> bool:
        row = session.get(TmdbPreviewIntent, int(item_id))
        if row is None or int(row.revision or 0) != int(revision):
            return False
        session.delete(row)
        session.flush()
        return True

    def release_state(self, session: Session, title_type: str, tmdb_id: int) -> TmdbPreviewReleaseState | None:
        return session.scalar(
            select(TmdbPreviewReleaseState).where(
                TmdbPreviewReleaseState.title_type == title_type,
                TmdbPreviewReleaseState.tmdb_id == int(tmdb_id),
            )
        )

    def upsert_release_state(
        self,
        session: Session,
        *,
        title_type: str,
        tmdb_id: int,
        title: str,
        release_at: datetime | None,
        list_count: int | None = None,
    ) -> TmdbPreviewReleaseState:
        row = self.release_state(session, title_type, tmdb_id)
        if row is None:
            row = TmdbPreviewReleaseState(title_type=title_type, tmdb_id=int(tmdb_id))
            session.add(row)
        row.title = str(title or "")
        row.release_at = release_at
        row.list_count = list_count
        session.flush()
        return row

    def list_release_states(self, session: Session) -> list[TmdbPreviewReleaseState]:
        return list(
            session.scalars(
                select(TmdbPreviewReleaseState).order_by(
                    TmdbPreviewReleaseState.release_at,
                    TmdbPreviewReleaseState.title,
                )
            )
        )

    def set_release_acknowledged(self, session: Session, title_type: str, tmdb_id: int, acknowledged: bool) -> bool:
        row = self.release_state(session, title_type, tmdb_id)
        if row is None:
            return False
        row.acknowledged_at = datetime.now(tz=UTC).replace(tzinfo=None) if acknowledged else None
        session.flush()
        return True

    def mark_release_sent(self, session: Session, title_type: str, tmdb_id: int) -> None:
        row = self.release_state(session, title_type, tmdb_id)
        if row is None:
            return
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        row.last_sent_at = now
        row.notify_count = int(row.notify_count or 0) + 1
        session.flush()

    def clear(self, session: Session) -> None:
        session.execute(delete(TmdbPreviewIntent))
        session.execute(delete(TmdbPreviewReleaseState))
        session.execute(delete(TmdbPreviewSnapshot))
        session.execute(delete(CatalogIdentityMap))


def _nullable_equals(column, value: int | None):
    return column.is_(None) if value is None else column == int(value)


def _json_value(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
