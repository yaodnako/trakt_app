from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from trakt_tracker.config import normalize_catalog_provider_mode
from trakt_tracker.domain import HistoryItemInput, RatingInput
from trakt_tracker.infrastructure.trakt.client import (
    TraktError,
    TraktMutationUncertain,
    TraktRateLimitError,
    TraktReauthorizationRequired,
)
from trakt_tracker.persistence.trakt_outbox import (
    OUTBOX_BLOCKED,
    OUTBOX_PENDING,
    OUTBOX_UNCERTAIN,
    ClaimedTraktOperation,
    TraktOutboxRepository,
)


TRAKT_OPERATION_HISTORY = "history"
TRAKT_OPERATION_RATING = "rating"
TRAKT_OPERATION_WATCHLIST = "watchlist"
TRAKT_OPERATION_RELEASE = "release"
TRAKT_OPERATION_PAUSED = "paused"
TRAKT_OPERATION_DROPPED = "dropped"

_LAST_SUCCESS_KEY = "trakt_outbox_last_success_at"
_LAST_ERROR_KEY = "trakt_outbox_last_error"
_LAST_ERROR_AT_KEY = "trakt_outbox_last_error_at"


@dataclass(frozen=True, slots=True)
class TraktSyncResult:
    processed: int
    delivered: int
    waiting: int
    blocked: int


class TraktOutboxService:
    """Owns durable Trakt intent and delivers it without holding SQLite transactions."""

    def __init__(self, db, auth_service, repository, episode_repository, sync_state) -> None:
        self._db = db
        self._auth = auth_service
        self._repository: TraktOutboxRepository = repository
        self._episodes = episode_repository
        self._sync_state = sync_state
        self._wake_callback: Callable[[], None] | None = None
        self._history_delivered_callback: Callable[[int], None] | None = None
        self._delivery_callback: Callable[[ClaimedTraktOperation], None] | None = None

    def set_wake_callback(self, callback: Callable[[], None] | None) -> None:
        self._wake_callback = callback

    def set_history_delivered_callback(self, callback: Callable[[int], None] | None) -> None:
        self._history_delivered_callback = callback

    def set_delivery_callback(self, callback: Callable[[ClaimedTraktOperation], None] | None) -> None:
        """Receive confirmed deliveries after the outbox row is committed."""
        self._delivery_callback = callback

    def wake(self) -> None:
        if self._trakt_mode_enabled() and self._wake_callback is not None:
            self._wake_callback()

    def _trakt_mode_enabled(self) -> bool:
        return normalize_catalog_provider_mode(
            getattr(self._auth.config, "catalog_provider_mode", "trakt")
        ) == "trakt"

    @staticmethod
    def history_key(
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        normalized_type = "show" if title_type == "show" else "movie"
        suffix = f":s{int(season)}:e{int(episode)}" if season is not None and episode is not None else ""
        return f"history:{normalized_type}:{int(trakt_id)}{suffix}"

    @staticmethod
    def rating_key(
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        normalized_type = "show" if title_type == "show" else "movie"
        suffix = f":s{int(season)}:e{int(episode)}" if season is not None and episode is not None else ""
        return f"rating:{normalized_type}:{int(trakt_id)}{suffix}"

    @staticmethod
    def membership_key(operation_type: str, title_type: str, trakt_id: int) -> str:
        normalized_type = "show" if title_type == "show" else "movie"
        return f"{operation_type}:{normalized_type}:{int(trakt_id)}"

    @staticmethod
    def hidden_key(operation_type: str, trakt_id: int) -> str:
        return f"{operation_type}:show:{int(trakt_id)}"

    def enqueue_history(
        self,
        session,
        *,
        title_type: str,
        trakt_id: int,
        title: str,
        desired_watched: bool,
        base_watched: bool,
        watched_at: datetime | None,
        season: int | None = None,
        episode: int | None = None,
        episode_trakt_id: int | None = None,
        origin: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if not self._trakt_mode_enabled():
            return None
        if desired_watched and watched_at is None:
            return None
        key = self.history_key(
            title_type=title_type,
            trakt_id=trakt_id,
            season=season,
            episode=episode,
        )
        normalized_watched_at = self._iso_utc(watched_at) if watched_at is not None else ""
        row = self._repository.enqueue(
            session,
            operation_key=key,
            operation_type=TRAKT_OPERATION_HISTORY,
            base_state={
                "watched": bool(base_watched),
                "watched_at": normalized_watched_at if base_watched else "",
            },
            desired_state={
                "watched": bool(desired_watched),
                "watched_at": normalized_watched_at if desired_watched else "",
            },
            payload={
                "title_type": "show" if title_type == "show" else "movie",
                "trakt_id": int(trakt_id),
                "episode_trakt_id": int(episode_trakt_id or 0),
                "season": int(season) if season is not None else None,
                "episode": int(episode) if episode is not None else None,
                "title": str(title or ""),
                "watched_at": normalized_watched_at,
                **dict(metadata or {}),
            },
            origin=origin,
        )
        return row.operation_key if row is not None else None

    def enqueue_rating(
        self,
        session,
        *,
        title_type: str,
        trakt_id: int,
        rating: int,
        base_rating: int | None,
        title: str = "",
        season: int | None = None,
        episode: int | None = None,
        episode_trakt_id: int | None = None,
    ) -> str | None:
        if not self._trakt_mode_enabled():
            return None
        key = self.rating_key(
            title_type=title_type,
            trakt_id=trakt_id,
            season=season,
            episode=episode,
        )
        row = self._repository.enqueue(
            session,
            operation_key=key,
            operation_type=TRAKT_OPERATION_RATING,
            base_state={"rating": base_rating},
            desired_state={"rating": int(rating)},
            payload={
                "title_type": "show" if title_type == "show" else "movie",
                "trakt_id": int(trakt_id),
                "episode_trakt_id": int(episode_trakt_id or 0),
                "season": int(season) if season is not None else None,
                "episode": int(episode) if episode is not None else None,
                "rating": int(rating),
                "title": str(title or ""),
            },
        )
        return row.operation_key if row is not None else None

    def enqueue_membership(
        self,
        session,
        *,
        operation_type: str,
        title_type: str,
        trakt_id: int,
        base_member: bool,
        desired_member: bool,
        snapshot: dict[str, Any] | None = None,
        dependency_key: str | None = None,
        origin: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if not self._trakt_mode_enabled():
            return None
        key = self.membership_key(operation_type, title_type, trakt_id)
        row = self._repository.enqueue(
            session,
            operation_key=key,
            operation_type=operation_type,
            base_state={"member": bool(base_member)},
            desired_state={"member": bool(desired_member)},
            payload={
                "title_type": "show" if title_type == "show" else "movie",
                "trakt_id": int(trakt_id),
                "snapshot": dict(snapshot or {}),
                **dict(metadata or {}),
            },
            dependency_key=dependency_key,
            origin=origin,
        )
        return row.operation_key if row is not None else None

    def enqueue_hidden(
        self,
        session,
        *,
        operation_type: str,
        trakt_id: int,
        base_hidden: bool,
        desired_hidden: bool,
    ) -> str | None:
        if not self._trakt_mode_enabled():
            return None
        key = self.hidden_key(operation_type, trakt_id)
        row = self._repository.enqueue(
            session,
            operation_key=key,
            operation_type=operation_type,
            base_state={"member": bool(base_hidden)},
            desired_state={"member": bool(desired_hidden)},
            payload={"title_type": "show", "trakt_id": int(trakt_id)},
        )
        return row.operation_key if row is not None else None

    def pending_history_dependency(self, *, title_type: str, trakt_id: int) -> str | None:
        prefix = f"history:{'show' if title_type == 'show' else 'movie'}:{int(trakt_id)}"
        with self._db.session() as session:
            rows = self._repository.list_items(session, operation_types=(TRAKT_OPERATION_HISTORY,))
            for row in rows:
                if row.operation_key == prefix or row.operation_key.startswith(f"{prefix}:"):
                    return f"scope:{prefix}"
        return None

    def drain(self, *, limit: int = 20) -> TraktSyncResult:
        if not self._trakt_mode_enabled():
            return self._result(processed=0, delivered=0)
        if not self._auth.is_authorized():
            self._record_error("Trakt authorization is unavailable; local changes remain queued.")
            return self._result(processed=0, delivered=0)
        with self._db.session() as session:
            claimed = self._repository.claim_due(session, limit=limit)
        delivered = 0
        batches: dict[tuple[str, bool | None], list[ClaimedTraktOperation]] = {}
        singles: list[ClaimedTraktOperation] = []
        for operation in claimed:
            batch_key = self._batch_key(operation)
            if batch_key is None:
                singles.append(operation)
            else:
                batches.setdefault(batch_key, []).append(operation)
        for operations in batches.values():
            if len(operations) == 1:
                singles.extend(operations)
            else:
                delivered += self._deliver_batch(operations)
        for operation in singles:
            if self._deliver_one(operation):
                delivered += 1
        return self._result(processed=len(claimed), delivered=delivered)

    def retry(self) -> TraktSyncResult:
        if not self._trakt_mode_enabled():
            return self._result(processed=0, delivered=0)
        with self._db.session() as session:
            self._repository.retry_all(session)
        self.wake()
        return self._result(processed=0, delivered=0)

    def discard_blocked(self, item_id: int) -> bool:
        with self._db.session() as session:
            return self._repository.discard_blocked(session, item_id)

    def status(self) -> dict[str, Any]:
        with self._db.session() as session:
            self._repository.recover_expired_leases(session)
            counts = self._repository.counts(session)
            rows = self._repository.list_items(session)
            next_attempt = self._repository.earliest_next_attempt(session)
            last_success = self._sync_state.get_value(session, _LAST_SUCCESS_KEY, "")
            last_error = self._sync_state.get_value(session, _LAST_ERROR_KEY, "")
            last_error_at = self._sync_state.get_value(session, _LAST_ERROR_AT_KEY, "")
        authorized = bool(self._auth.is_authorized())
        last_cache_at = ""
        try:
            client = self._auth.get_client()
            cache_reader = getattr(client, "last_good_cache_at", None)
            cached_at = cache_reader() if callable(cache_reader) else None
            if cached_at is not None:
                last_cache_at = self._iso_utc(cached_at)
        except Exception:
            last_cache_at = ""
        if counts["blocked"]:
            mode = "attention"
        elif counts["sending"]:
            mode = "syncing"
        elif not authorized or counts["waiting"]:
            mode = "local"
        else:
            mode = "synced"
        return {
            "mode": mode,
            "authorized": authorized,
            "pending": counts["waiting"],
            "blocked": counts["blocked"],
            "sending": counts["sending"],
            "uncertain": counts["uncertain"],
            "total": counts["total"],
            "last_success_at": last_success,
            "last_error": last_error,
            "last_error_at": last_error_at,
            "next_attempt_at": self._iso_utc(next_attempt) if next_attempt is not None else "",
            "last_cache_at": last_cache_at,
            "items": [
                {
                    "id": int(row.id),
                    "operation_key": row.operation_key,
                    "operation_type": row.operation_type,
                    "status": row.status,
                    "attempt_count": int(row.attempt_count or 0),
                    "last_error": row.last_error,
                    "updated_at": self._iso_utc(row.updated_at),
                }
                for row in rows
            ],
        }

    def mutation_metadata(self) -> dict[str, Any]:
        with self._db.session() as session:
            counts = self._repository.counts(session)
        if counts["blocked"]:
            mode = "attention"
        elif counts["sending"]:
            mode = "syncing"
        elif not self._auth.is_authorized() or counts["waiting"]:
            mode = "local"
        else:
            mode = "synced"
        return {"sync_state": mode, "pending_count": counts["total"]}

    def overlay_membership(
        self,
        values: set[tuple[str, int]],
        *,
        operation_type: str,
    ) -> set[tuple[str, int]]:
        result = set(values)
        with self._db.session() as session:
            rows = self._repository.list_items(session, operation_types=(operation_type,))
            for row in rows:
                payload = self._repository._load_dict(row.payload_json)
                desired = self._repository._load_dict(row.desired_state_json)
                key = (str(payload.get("title_type") or ""), int(payload.get("trakt_id") or 0))
                if desired.get("member"):
                    result.add(key)
                else:
                    result.discard(key)
        return result

    def membership_intents(self, *, operation_type: str) -> list[tuple[dict[str, Any], bool]]:
        result: list[tuple[dict[str, Any], bool]] = []
        with self._db.session() as session:
            rows = self._repository.list_items(session, operation_types=(operation_type,))
            for row in rows:
                payload = self._repository._load_dict(row.payload_json)
                desired = self._repository._load_dict(row.desired_state_json)
                result.append((payload, bool(desired.get("member"))))
        return result

    def intents(self, *, operation_type: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        result: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with self._db.session() as session:
            rows = self._repository.list_items(session, operation_types=(operation_type,))
            for row in rows:
                result.append(
                    (
                        self._repository._load_dict(row.payload_json),
                        self._repository._load_dict(row.desired_state_json),
                    )
                )
        return result

    def overlay_hidden(self, values: set[int], *, operation_type: str) -> set[int]:
        result = set(values)
        with self._db.session() as session:
            rows = self._repository.list_items(session, operation_types=(operation_type,))
            for row in rows:
                payload = self._repository._load_dict(row.payload_json)
                desired = self._repository._load_dict(row.desired_state_json)
                trakt_id = int(payload.get("trakt_id") or 0)
                if desired.get("member"):
                    result.add(trakt_id)
                else:
                    result.discard(trakt_id)
        return result

    def _deliver_one(self, operation: ClaimedTraktOperation) -> bool:
        try:
            client = self._auth.get_client()
            if operation.claimed_from_status == OUTBOX_UNCERTAIN:
                if self._remote_matches_desired(client, operation):
                    completed = self._complete(operation)
                    if completed:
                        self._notify_history_delivered(operation)
                    return completed
            response = self._dispatch(client, operation)
            if self._contains_not_found(response):
                self._reschedule(
                    operation,
                    status=OUTBOX_BLOCKED,
                    error="Trakt did not recognize one or more queued objects.",
                    next_attempt_at=None,
                )
                return False
        except Exception as exc:
            self._handle_delivery_error(operation, exc)
            return False

        completed = self._complete(operation)
        if completed:
            self._notify_history_delivered(operation)
        return completed

    def _deliver_batch(self, operations: list[ClaimedTraktOperation]) -> int:
        try:
            client = self._auth.get_client()
            operation_type = operations[0].operation_type
            if operation_type == TRAKT_OPERATION_HISTORY:
                items = [self._history_input(client, operation.payload) for operation in operations]
                desired = operations[0].desired_state if isinstance(operations[0].desired_state, dict) else {}
                response = (
                    client.add_history_items(items)
                    if desired.get("watched")
                    else client.remove_history_items(items)
                )
            elif operation_type == TRAKT_OPERATION_RATING and callable(getattr(client, "set_ratings", None)):
                items = [self._rating_input(client, operation) for operation in operations]
                response = client.set_ratings(items)
            else:
                return sum(1 for operation in operations if self._deliver_one(operation))
            if self._contains_not_found(response):
                for operation in operations:
                    self._reschedule(
                        operation,
                        status=OUTBOX_BLOCKED,
                        error="Trakt did not recognize one or more objects in the delivered batch.",
                        next_attempt_at=None,
                    )
                return 0
        except Exception as exc:
            for operation in operations:
                self._handle_delivery_error(operation, exc)
            return 0

        delivered = 0
        refreshed_show_ids: set[int] = set()
        for operation in operations:
            if self._complete(operation):
                delivered += 1
                show_id = int(operation.payload.get("trakt_id") or 0)
                is_show_history = (
                    operation.operation_type == TRAKT_OPERATION_HISTORY
                    and str(operation.payload.get("title_type") or "") == "show"
                )
                if is_show_history and show_id not in refreshed_show_ids:
                    self._notify_history_delivered(operation)
                    refreshed_show_ids.add(show_id)
        return delivered

    def _handle_delivery_error(self, operation: ClaimedTraktOperation, exc: Exception) -> None:
        if isinstance(exc, TraktRateLimitError):
            delay = max(1, int(exc.retry_after_seconds or self._backoff_seconds(operation.attempt_count)))
            self._reschedule(
                operation,
                status=OUTBOX_PENDING,
                error=str(exc),
                next_attempt_at=datetime.utcnow() + timedelta(seconds=delay),
            )
            return
        if isinstance(exc, TraktReauthorizationRequired):
            self._reschedule(
                operation,
                status=OUTBOX_PENDING,
                error=str(exc),
                next_attempt_at=datetime.utcnow() + timedelta(minutes=5),
            )
            return
        if isinstance(exc, ValueError):
            self._reschedule(operation, status=OUTBOX_BLOCKED, error=str(exc), next_attempt_at=None)
            return
        if isinstance(exc, TraktMutationUncertain):
            self._reschedule(
                operation,
                status=OUTBOX_UNCERTAIN,
                error=str(exc),
                next_attempt_at=datetime.utcnow() + timedelta(seconds=self._backoff_seconds(operation.attempt_count)),
            )
            return
        if isinstance(exc, TraktError):
            status_code = self._status_code(exc)
            if status_code == 401 or "Authentication is required" in str(exc):
                state = OUTBOX_PENDING
                next_attempt = datetime.utcnow() + timedelta(minutes=5)
            elif status_code is not None and 400 <= status_code < 500:
                state = OUTBOX_BLOCKED
                next_attempt = None
            else:
                # Plain TraktError is raised by authoritative/preflight reads. A
                # mutation that may have reached Trakt uses TraktMutationUncertain.
                state = OUTBOX_PENDING
                next_attempt = datetime.utcnow() + timedelta(seconds=self._backoff_seconds(operation.attempt_count))
            self._reschedule(operation, status=state, error=str(exc), next_attempt_at=next_attempt)
            return
        self._reschedule(
            operation,
            status=OUTBOX_UNCERTAIN,
            error=str(exc),
            next_attempt_at=datetime.utcnow() + timedelta(seconds=self._backoff_seconds(operation.attempt_count)),
        )

    @staticmethod
    def _batch_key(operation: ClaimedTraktOperation) -> tuple[str, bool | None] | None:
        if operation.claimed_from_status == OUTBOX_UNCERTAIN:
            return None
        payload = operation.payload
        is_episode_without_remote_id = (
            str(payload.get("title_type") or "") == "show"
            and payload.get("season") is not None
            and not int(payload.get("episode_trakt_id") or 0)
        )
        if is_episode_without_remote_id:
            return None
        desired = operation.desired_state if isinstance(operation.desired_state, dict) else {}
        if operation.operation_type == TRAKT_OPERATION_HISTORY:
            return operation.operation_type, bool(desired.get("watched"))
        if operation.operation_type == TRAKT_OPERATION_RATING:
            return operation.operation_type, None
        return None

    def _dispatch(self, client, operation: ClaimedTraktOperation) -> Any:
        desired = operation.desired_state if isinstance(operation.desired_state, dict) else {}
        payload = operation.payload
        if operation.operation_type == TRAKT_OPERATION_HISTORY:
            item = self._history_input(client, payload)
            return client.add_history_items([item]) if desired.get("watched") else client.remove_history_items([item])
        if operation.operation_type == TRAKT_OPERATION_RATING:
            return client.set_rating(self._rating_input(client, operation))
        if operation.operation_type == TRAKT_OPERATION_WATCHLIST:
            return client.set_watchlist(
                str(payload.get("title_type") or "movie"),
                int(payload.get("trakt_id") or 0),
                watchlisted=bool(desired.get("member")),
            )
        if operation.operation_type == TRAKT_OPERATION_RELEASE:
            return client.set_release_tracking(
                str(payload.get("title_type") or "movie"),
                int(payload.get("trakt_id") or 0),
                tracked=bool(desired.get("member")),
            )
        if operation.operation_type == TRAKT_OPERATION_PAUSED:
            method = client.add_paused_show if desired.get("member") else client.remove_paused_show
            return method(int(payload.get("trakt_id") or 0))
        if operation.operation_type == TRAKT_OPERATION_DROPPED:
            method = client.add_dropped_show if desired.get("member") else client.remove_dropped_show
            return method(int(payload.get("trakt_id") or 0))
        raise ValueError(f"Unsupported Trakt outbox operation: {operation.operation_type}")

    def _remote_matches_desired(self, client, operation: ClaimedTraktOperation) -> bool:
        payload = operation.payload
        desired = operation.desired_state if isinstance(operation.desired_state, dict) else {}
        if operation.operation_type == TRAKT_OPERATION_HISTORY:
            return self._history_matches(client, payload, desired)
        if operation.operation_type == TRAKT_OPERATION_RATING:
            return self._rating_matches(client, payload, desired)
        if operation.operation_type == TRAKT_OPERATION_WATCHLIST:
            keys = {
                (item.title_type, int(item.trakt_id))
                for item in self._authoritative_call(client.get_watchlist)
            }
            member = (str(payload.get("title_type") or ""), int(payload.get("trakt_id") or 0)) in keys
            return member is bool(desired.get("member"))
        if operation.operation_type == TRAKT_OPERATION_RELEASE:
            keys = {
                (item.title_type, int(item.trakt_id))
                for item in self._authoritative_call(client.get_release_tracking)
            }
            member = (str(payload.get("title_type") or ""), int(payload.get("trakt_id") or 0)) in keys
            return member is bool(desired.get("member"))
        if operation.operation_type in {TRAKT_OPERATION_PAUSED, TRAKT_OPERATION_DROPPED}:
            fetch = client.get_paused_shows if operation.operation_type == TRAKT_OPERATION_PAUSED else client.get_dropped_shows
            ids = self._hidden_ids(fetch)
            return (int(payload.get("trakt_id") or 0) in ids) is bool(desired.get("member"))
        return False

    def _history_matches(self, client, payload: dict[str, Any], desired: dict[str, Any]) -> bool:
        remote_id = self._remote_trakt_id(client, payload)
        title_type = str(payload.get("title_type") or "movie")
        matches: list[dict[str, Any]] = []
        page = 1
        while True:
            batch, headers = client.get_watch_history_page(title_type, limit=1000, page=page)
            for item in batch:
                if not isinstance(item, dict):
                    continue
                entity = item.get("movie") if title_type == "movie" else item.get("episode")
                ids = entity.get("ids", {}) if isinstance(entity, dict) else {}
                if int(ids.get("trakt") or 0) == remote_id:
                    matches.append(item)
            if not self._has_next_page(batch, headers, page):
                break
            page += 1
        if not desired.get("watched"):
            return not matches
        target = str(desired.get("watched_at") or payload.get("watched_at") or "")
        return any(self._normalize_iso(item.get("watched_at")) == self._normalize_iso(target) for item in matches)

    def _rating_matches(self, client, payload: dict[str, Any], desired: dict[str, Any]) -> bool:
        remote_id = self._remote_trakt_id(client, payload)
        title_type = str(payload.get("title_type") or "movie")
        page = 1
        while True:
            batch = self._authoritative_call(client.get_ratings, title_type, limit=100, page=page)
            if not isinstance(batch, list):
                batch = []
            for item in batch:
                if not isinstance(item, dict):
                    continue
                entity = item.get("movie") if title_type == "movie" else (
                    item.get("episode") if payload.get("season") is not None else item.get("show")
                )
                ids = entity.get("ids", {}) if isinstance(entity, dict) else {}
                if int(ids.get("trakt") or 0) == remote_id:
                    return int(item.get("rating") or 0) == int(desired.get("rating") or 0)
            if len(batch) < 100:
                break
            page += 1
        return False

    def _history_input(self, client, payload: dict[str, Any]) -> HistoryItemInput:
        watched_at = self._parse_datetime(payload.get("watched_at"))
        return HistoryItemInput(
            title_type=str(payload.get("title_type") or "movie"),
            trakt_id=self._remote_trakt_id(client, payload),
            watched_at=watched_at,
            season=payload.get("season"),
            episode=payload.get("episode"),
            title=str(payload.get("title") or ""),
        )

    def _rating_input(self, client, operation: ClaimedTraktOperation) -> RatingInput:
        desired = operation.desired_state if isinstance(operation.desired_state, dict) else {}
        payload = operation.payload
        return RatingInput(
            title_type=str(payload.get("title_type") or "movie"),
            trakt_id=self._remote_trakt_id(client, payload),
            rating=int(desired.get("rating") or payload.get("rating") or 0),
            season=payload.get("season"),
            episode=payload.get("episode"),
        )

    def _remote_trakt_id(self, client, payload: dict[str, Any]) -> int:
        if str(payload.get("title_type") or "movie") != "show" or payload.get("season") is None:
            return int(payload.get("trakt_id") or 0)
        cached_id = int(payload.get("episode_trakt_id") or 0)
        if cached_id:
            return cached_id
        show_id = int(payload.get("trakt_id") or 0)
        season = int(payload.get("season") or 0)
        episode = int(payload.get("episode") or 0)
        with self._db.session() as session:
            row = self._episodes.find_episode(session, show_id, season, episode)
            if row is not None and int(row.episode_trakt_id or 0):
                return int(row.episode_trakt_id)
        details = client.get_episode_details(show_id, season, episode, use_cache=False)
        if details is None or not int(details.trakt_id or 0):
            raise ValueError("Episode metadata was not found for the queued Trakt operation.")
        with self._db.session() as session:
            self._episodes.upsert_episode(session, show_id, details)
        return int(details.trakt_id)

    def _complete(self, operation: ClaimedTraktOperation) -> bool:
        with self._db.session() as session:
            completed = self._repository.complete(session, operation)
            if completed:
                self._sync_state.set_value(session, _LAST_SUCCESS_KEY, self._iso_utc(datetime.utcnow()))
                self._sync_state.set_value(session, _LAST_ERROR_KEY, "")
        if completed and self._delivery_callback is not None:
            try:
                self._delivery_callback(operation)
            except Exception:
                pass
        return completed

    def _notify_history_delivered(self, operation: ClaimedTraktOperation) -> None:
        if (
            operation.operation_type != TRAKT_OPERATION_HISTORY
            or str(operation.payload.get("title_type") or "") != "show"
            or self._history_delivered_callback is None
        ):
            return
        try:
            self._history_delivered_callback(int(operation.payload.get("trakt_id") or 0))
        except Exception:
            pass

    def _reschedule(
        self,
        operation: ClaimedTraktOperation,
        *,
        status: str,
        error: str,
        next_attempt_at: datetime | None,
    ) -> None:
        with self._db.session() as session:
            self._repository.reschedule(
                session,
                operation,
                status=status,
                error=error,
                next_attempt_at=next_attempt_at,
            )
            self._sync_state.set_value(session, _LAST_ERROR_KEY, str(error or "")[:4000])
            self._sync_state.set_value(session, _LAST_ERROR_AT_KEY, self._iso_utc(datetime.utcnow()))

    def _record_error(self, error: str) -> None:
        with self._db.session() as session:
            self._sync_state.set_value(session, _LAST_ERROR_KEY, error)
            self._sync_state.set_value(session, _LAST_ERROR_AT_KEY, self._iso_utc(datetime.utcnow()))

    def _result(self, *, processed: int, delivered: int) -> TraktSyncResult:
        with self._db.session() as session:
            counts = self._repository.counts(session)
        return TraktSyncResult(
            processed=processed,
            delivered=delivered,
            waiting=counts["waiting"],
            blocked=counts["blocked"],
        )

    @staticmethod
    def _contains_not_found(response: Any) -> bool:
        if not isinstance(response, dict) or "not_found" not in response:
            return False
        missing = response.get("not_found")
        if isinstance(missing, dict):
            return any(bool(value) for value in missing.values())
        return bool(missing)

    @staticmethod
    def _hidden_ids(fetch_page) -> set[int]:
        result: set[int] = set()
        page = 1
        while True:
            try:
                batch = fetch_page(limit=100, page=page, authoritative=True)
            except TypeError as exc:
                if "authoritative" not in str(exc):
                    raise
                batch = fetch_page(limit=100, page=page)
            if not isinstance(batch, list):
                batch = []
            for item in batch:
                show = item.get("show", {}) if isinstance(item, dict) else {}
                ids = show.get("ids", {}) if isinstance(show, dict) else {}
                if ids.get("trakt"):
                    result.add(int(ids["trakt"]))
            if len(batch) < 100:
                break
            page += 1
        return result

    @staticmethod
    def _authoritative_call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs, authoritative=True)
        except TypeError as exc:
            if "authoritative" not in str(exc):
                raise
            return method(*args, **kwargs)

    @staticmethod
    def _has_next_page(batch: list[Any], headers: dict[str, str], page: int) -> bool:
        try:
            page_count = int(headers.get("X-Pagination-Page-Count") or headers.get("x-pagination-page-count") or 0)
        except (TypeError, ValueError):
            page_count = 0
        return page < page_count if page_count else len(batch) >= 1000

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        value = getattr(exc, "status_code", None)
        if isinstance(value, int):
            return value
        text = str(exc)
        marker = "Trakt request failed: "
        if marker not in text:
            return None
        try:
            return int(text.split(marker, 1)[1].split(None, 1)[0])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _backoff_seconds(attempt: int) -> int:
        return min(1800, 15 * (2 ** min(7, max(0, int(attempt) - 1))))

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))

    @classmethod
    def _normalize_iso(cls, value: Any) -> str:
        parsed = cls._parse_datetime(value)
        return cls._iso_utc(parsed) if parsed is not None else ""

    @staticmethod
    def _iso_utc(value: datetime) -> str:
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return aware.isoformat().replace("+00:00", "Z")
