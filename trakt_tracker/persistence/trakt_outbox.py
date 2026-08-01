from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from .models import TraktOutboxItem


OUTBOX_PENDING = "pending"
OUTBOX_SENDING = "sending"
OUTBOX_UNCERTAIN = "uncertain"
OUTBOX_BLOCKED = "blocked"
OUTBOX_ACTIVE_STATUSES = (OUTBOX_PENDING, OUTBOX_SENDING, OUTBOX_UNCERTAIN, OUTBOX_BLOCKED)


@dataclass(frozen=True, slots=True)
class ClaimedTraktOperation:
    id: int
    operation_key: str
    operation_type: str
    base_state: Any
    desired_state: Any
    payload: dict[str, Any]
    revision: int
    dependency_key: str | None
    origin: str
    claimed_from_status: str
    attempt_count: int
    lease_token: str


class TraktOutboxRepository:
    """Persistence primitives for the per-profile Trakt mutation outbox."""

    def enqueue(
        self,
        session: Session,
        *,
        operation_key: str,
        operation_type: str,
        base_state: Any,
        desired_state: Any,
        payload: dict[str, Any],
        dependency_key: str | None = None,
        origin: str = "user",
    ) -> TraktOutboxItem | None:
        now = datetime.utcnow()
        base_json = self._dump(base_state)
        desired_json = self._dump(desired_state)
        payload_json = self._dump(payload)
        row = session.scalar(select(TraktOutboxItem).where(TraktOutboxItem.operation_key == operation_key))
        if row is None:
            if base_json == desired_json:
                return None
            row = TraktOutboxItem(
                operation_key=operation_key,
                operation_type=operation_type,
                base_state_json=base_json,
                desired_state_json=desired_json,
                payload_json=payload_json,
                dependency_key=dependency_key,
                origin=origin,
                status=OUTBOX_PENDING,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return row

        if row.desired_state_json == desired_json and row.payload_json == payload_json:
            return row

        guaranteed_unsent = (
            row.status == OUTBOX_PENDING
            and int(row.attempt_count or 0) == 0
            and not row.lease_token
        )
        if desired_json == row.base_state_json and guaranteed_unsent:
            session.delete(row)
            session.flush()
            return None

        row.operation_type = operation_type
        row.desired_state_json = desired_json
        row.payload_json = payload_json
        row.dependency_key = dependency_key
        row.origin = origin
        row.revision = int(row.revision or 0) + 1
        row.updated_at = now
        if row.status not in {OUTBOX_SENDING, OUTBOX_UNCERTAIN}:
            row.status = OUTBOX_PENDING
            row.next_attempt_at = now
            row.last_error = ""
            row.lease_token = None
            row.lease_expires_at = None
        session.flush()
        return row

    def get(self, session: Session, operation_key: str) -> TraktOutboxItem | None:
        return session.scalar(select(TraktOutboxItem).where(TraktOutboxItem.operation_key == operation_key))

    def get_by_id(self, session: Session, item_id: int) -> TraktOutboxItem | None:
        return session.get(TraktOutboxItem, int(item_id))

    def list_items(
        self,
        session: Session,
        *,
        statuses: tuple[str, ...] | None = None,
        operation_types: tuple[str, ...] | None = None,
    ) -> list[TraktOutboxItem]:
        stmt = select(TraktOutboxItem)
        if statuses:
            stmt = stmt.where(TraktOutboxItem.status.in_(statuses))
        if operation_types:
            stmt = stmt.where(TraktOutboxItem.operation_type.in_(operation_types))
        return list(session.scalars(stmt.order_by(TraktOutboxItem.created_at, TraktOutboxItem.id)))

    def recover_expired_leases(self, session: Session, *, now: datetime | None = None) -> int:
        current = now or datetime.utcnow()
        result = session.execute(
            update(TraktOutboxItem)
            .where(TraktOutboxItem.status == OUTBOX_SENDING)
            .where(
                or_(
                    TraktOutboxItem.lease_expires_at.is_(None),
                    TraktOutboxItem.lease_expires_at <= current,
                )
            )
            .values(
                status=OUTBOX_UNCERTAIN,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=current,
                last_error="Delivery result is unknown after an interrupted worker.",
                updated_at=current,
            )
        )
        return int(result.rowcount or 0)

    def claim_due(
        self,
        session: Session,
        *,
        limit: int = 20,
        lease_seconds: int = 120,
        now: datetime | None = None,
    ) -> list[ClaimedTraktOperation]:
        current = now or datetime.utcnow()
        self.recover_expired_leases(session, now=current)
        candidates = list(
            session.scalars(
                select(TraktOutboxItem)
                .where(TraktOutboxItem.status.in_((OUTBOX_PENDING, OUTBOX_UNCERTAIN)))
                .where(
                    or_(
                        TraktOutboxItem.next_attempt_at.is_(None),
                        TraktOutboxItem.next_attempt_at <= current,
                    )
                )
                .order_by(TraktOutboxItem.created_at, TraktOutboxItem.id)
                .limit(max(1, int(limit)) * 4)
            )
        )
        claimed: list[ClaimedTraktOperation] = []
        for row in candidates:
            if len(claimed) >= max(1, int(limit)):
                break
            if row.dependency_key and self._dependency_exists(session, row.dependency_key):
                continue
            previous_status = row.status
            revision = int(row.revision)
            lease_token = token_urlsafe(24)
            result = session.execute(
                update(TraktOutboxItem)
                .where(TraktOutboxItem.id == row.id)
                .where(TraktOutboxItem.revision == revision)
                .where(TraktOutboxItem.status == previous_status)
                .values(
                    status=OUTBOX_SENDING,
                    lease_token=lease_token,
                    lease_expires_at=current + timedelta(seconds=max(10, int(lease_seconds))),
                    attempt_count=int(row.attempt_count or 0) + 1,
                    last_attempt_at=current,
                    updated_at=current,
                )
            )
            if int(result.rowcount or 0) != 1:
                continue
            claimed.append(
                ClaimedTraktOperation(
                    id=int(row.id),
                    operation_key=row.operation_key,
                    operation_type=row.operation_type,
                    base_state=self._load(row.base_state_json),
                    desired_state=self._load(row.desired_state_json),
                    payload=self._load_dict(row.payload_json),
                    revision=revision,
                    dependency_key=row.dependency_key,
                    origin=row.origin,
                    claimed_from_status=previous_status,
                    attempt_count=int(row.attempt_count or 0) + 1,
                    lease_token=lease_token,
                )
            )
        session.flush()
        return claimed

    def _dependency_exists(self, session: Session, dependency_key: str) -> bool:
        if dependency_key.startswith("scope:"):
            operation_prefix = dependency_key.removeprefix("scope:")
            return session.scalar(
                select(TraktOutboxItem.id)
                .where(
                    or_(
                        TraktOutboxItem.operation_key == operation_prefix,
                        TraktOutboxItem.operation_key.startswith(f"{operation_prefix}:"),
                    )
                )
                .limit(1)
            ) is not None
        return self.get(session, dependency_key) is not None

    def complete(self, session: Session, claimed: ClaimedTraktOperation) -> bool:
        result = session.execute(
            delete(TraktOutboxItem)
            .where(TraktOutboxItem.id == claimed.id)
            .where(TraktOutboxItem.revision == claimed.revision)
            .where(TraktOutboxItem.lease_token == claimed.lease_token)
        )
        if int(result.rowcount or 0) == 1:
            return True
        self._mark_newer_revision_uncertain(session, claimed)
        return False

    def reschedule(
        self,
        session: Session,
        claimed: ClaimedTraktOperation,
        *,
        status: str,
        error: str,
        next_attempt_at: datetime | None,
    ) -> bool:
        current = datetime.utcnow()
        result = session.execute(
            update(TraktOutboxItem)
            .where(TraktOutboxItem.id == claimed.id)
            .where(TraktOutboxItem.revision == claimed.revision)
            .where(TraktOutboxItem.lease_token == claimed.lease_token)
            .values(
                status=status,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=next_attempt_at,
                last_error=str(error or "")[:4000],
                updated_at=current,
            )
        )
        if int(result.rowcount or 0) == 1:
            return True
        self._mark_newer_revision_uncertain(session, claimed, error=error)
        return False

    def mark_reconciled_not_applied(self, session: Session, claimed: ClaimedTraktOperation) -> bool:
        return self.reschedule(
            session,
            claimed,
            status=OUTBOX_PENDING,
            error="",
            next_attempt_at=datetime.utcnow(),
        )

    def retry_all(self, session: Session) -> int:
        now = datetime.utcnow()
        pending = session.execute(
            update(TraktOutboxItem)
            .where(TraktOutboxItem.status == OUTBOX_PENDING)
            .values(next_attempt_at=now, last_error="", updated_at=now)
        )
        uncertain = session.execute(
            update(TraktOutboxItem)
            .where(TraktOutboxItem.status == OUTBOX_UNCERTAIN)
            .values(next_attempt_at=now, updated_at=now)
        )
        blocked = session.execute(
            update(TraktOutboxItem)
            .where(TraktOutboxItem.status == OUTBOX_BLOCKED)
            .values(status=OUTBOX_PENDING, next_attempt_at=now, last_error="", updated_at=now)
        )
        return sum(int(result.rowcount or 0) for result in (pending, uncertain, blocked))

    def discard_blocked(self, session: Session, item_id: int) -> bool:
        result = session.execute(
            delete(TraktOutboxItem)
            .where(TraktOutboxItem.id == int(item_id))
            .where(TraktOutboxItem.status == OUTBOX_BLOCKED)
        )
        return int(result.rowcount or 0) == 1

    def counts(self, session: Session) -> dict[str, int]:
        counts = {status: 0 for status in OUTBOX_ACTIVE_STATUSES}
        for status, count in session.execute(
            select(TraktOutboxItem.status, func.count(TraktOutboxItem.id)).group_by(TraktOutboxItem.status)
        ):
            counts[str(status)] = int(count or 0)
        counts["waiting"] = counts[OUTBOX_PENDING] + counts[OUTBOX_SENDING] + counts[OUTBOX_UNCERTAIN]
        counts["total"] = counts["waiting"] + counts[OUTBOX_BLOCKED]
        return counts

    def earliest_next_attempt(self, session: Session) -> datetime | None:
        return session.scalar(
            select(func.min(TraktOutboxItem.next_attempt_at)).where(
                TraktOutboxItem.status.in_((OUTBOX_PENDING, OUTBOX_UNCERTAIN))
            )
        )

    def _mark_newer_revision_uncertain(
        self,
        session: Session,
        claimed: ClaimedTraktOperation,
        *,
        error: str = "Intent changed while an earlier revision was being delivered.",
    ) -> None:
        now = datetime.utcnow()
        session.execute(
            update(TraktOutboxItem)
            .where(TraktOutboxItem.id == claimed.id)
            .where(TraktOutboxItem.lease_token == claimed.lease_token)
            .values(
                status=OUTBOX_UNCERTAIN,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=now,
                last_error=str(error or "")[:4000],
                updated_at=now,
            )
        )

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None

    @classmethod
    def _load_dict(cls, value: str) -> dict[str, Any]:
        parsed = cls._load(value)
        return parsed if isinstance(parsed, dict) else {}
