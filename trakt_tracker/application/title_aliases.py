from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
)
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import (
    TitleAliasRepository,
    TitleAliasTarget,
    normalize_title_search,
)


@dataclass(frozen=True, slots=True)
class TitleAliasRefreshResult:
    attempted: int = 0
    ready: int = 0
    no_data: int = 0
    failed: int = 0


class TitleAliasService:
    READY_REFRESH_AFTER = timedelta(days=180)
    NO_DATA_REFRESH_AFTER = timedelta(days=30)
    FAILURE_RETRY_AFTER = timedelta(days=1)

    def __init__(self, db: Database, auth, repository: TitleAliasRepository) -> None:
        self._db = db
        self._auth = auth
        self._repository = repository
        self._refresh_lock = Lock()

    def has_due_history_titles(self, *, language: str = "ru", now: datetime | None = None) -> bool:
        if not self._auth.is_authorized():
            return False
        checked_at = self._normalize_datetime(now or datetime.now(tz=UTC))
        with self._db.session() as session:
            targets = self._repository.list_history_targets(session, language=language)
        return any(self._is_due(target, checked_at) for target in targets)

    def refresh_due_history_titles(
        self,
        *,
        language: str = "ru",
        now: datetime | None = None,
    ) -> TitleAliasRefreshResult:
        if not self._auth.is_authorized() or not self._refresh_lock.acquire(blocking=False):
            return TitleAliasRefreshResult()
        try:
            return self._refresh_due_history_titles(language=language, now=now)
        finally:
            self._refresh_lock.release()

    def _refresh_due_history_titles(
        self,
        *,
        language: str,
        now: datetime | None,
    ) -> TitleAliasRefreshResult:
        checked_at = self._normalize_datetime(now or datetime.now(tz=UTC))
        with self._db.session() as session:
            targets = self._repository.list_history_targets(session, language=language)
        due_targets = [target for target in targets if self._is_due(target, checked_at)]
        if not due_targets:
            return TitleAliasRefreshResult()

        client = self._auth.get_client()
        ready = 0
        no_data = 0
        failed = 0
        for target in due_targets:
            try:
                translations = client.get_title_translations(target.trakt_id, target.title_type, language)
                canonical = normalize_title_search(target.title)
                aliases = []
                for item in translations:
                    if not isinstance(item, dict):
                        continue
                    alias = str(item.get("title", "") or "").strip()
                    normalized_alias = normalize_title_search(alias)
                    if normalized_alias and normalized_alias != canonical:
                        aliases.append(alias)
                with self._db.session() as session:
                    self._repository.replace_trakt_aliases(
                        session,
                        title_type=target.title_type,
                        trakt_id=target.trakt_id,
                        language=language,
                        aliases=aliases,
                        checked_at=checked_at,
                    )
                if aliases:
                    ready += 1
                else:
                    no_data += 1
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to refresh %s title aliases for %s %s",
                    language,
                    target.title_type,
                    target.trakt_id,
                )
                with self._db.session() as session:
                    self._repository.mark_retryable_failure(
                        session,
                        title_type=target.title_type,
                        trakt_id=target.trakt_id,
                        language=language,
                        checked_at=checked_at,
                    )
                failed += 1
        return TitleAliasRefreshResult(
            attempted=len(due_targets),
            ready=ready,
            no_data=no_data,
            failed=failed,
        )

    @classmethod
    def _is_due(cls, target: TitleAliasTarget, now: datetime) -> bool:
        last_checked_at = cls._normalize_datetime(target.last_checked_at)
        if last_checked_at is None:
            return True
        if target.status == ENRICH_STATUS_READY:
            refresh_after = cls.READY_REFRESH_AFTER
        elif target.status == ENRICH_STATUS_CHECKED_NO_DATA:
            refresh_after = cls.NO_DATA_REFRESH_AFTER
        elif target.status == ENRICH_STATUS_RETRYABLE_FAILURE:
            refresh_after = cls.FAILURE_RETRY_AFTER
        else:
            return True
        return now - last_checked_at >= refresh_after

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
