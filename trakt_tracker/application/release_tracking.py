from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
import time
from typing import Callable

from trakt_tracker.application.enrich_state import ENRICH_STATUS_UNKNOWN
from trakt_tracker.domain import ProgressView, TitleSummary, TitleType
from trakt_tracker.infrastructure.notifications import NotificationMessage


class ReleaseTrackingService:
    def __init__(self, db, auth_service, config_store, repository, progress_repository, sender, *, titles=None) -> None:
        self._db = db
        self._auth = auth_service
        self._config_store = config_store
        self._repository = repository
        self._progress_repository = progress_repository
        self._sender = sender
        self._titles = titles
        self._list_count_refresh_lock = Lock()
        self._list_count_refreshed_at = 0.0
        self._notification_state_callback: Callable[[], None] | None = None

    def set_notification_state_callback(self, callback: Callable[[], None]) -> None:
        self._notification_state_callback = callback

    def _notify_notification_state_changed(self) -> None:
        if self._notification_state_callback is not None:
            self._notification_state_callback()

    def refresh(self) -> list:
        items = self._auth.get_client().get_release_tracking()
        with self._db.session() as session:
            if self._titles is not None:
                for item in items:
                    self._titles.upsert_title(session, item)
            self._repository.sync_items(session, items)
            state = {(row.title_type, int(row.trakt_id)): row for row in self._repository.list_all(session)}
            for item in items:
                row = state.get((item.title_type, int(item.trakt_id)))
                item.is_release_tracked = True
                item.release_acknowledged = bool(row and row.acknowledged_at is not None)
                item.explore_metric_kind = "lists"
                item.explore_metric_count = row.list_count if row is not None else None
        return items

    def local_items(self) -> list[TitleSummary]:
        with self._db.session() as session:
            rows = self._repository.list_all(session)
            stored = (
                self._titles.by_trakt_ids(session, [int(row.trakt_id) for row in rows])
                if self._titles is not None
                else {}
            )
            return [self._local_item(row, stored.get(int(row.trakt_id))) for row in rows]

    @staticmethod
    def _local_item(row, title_row) -> TitleSummary:
        title_type: TitleType = "movie" if row.title_type == "movie" else "show"
        return TitleSummary(
            trakt_id=int(row.trakt_id),
            title_type=title_type,
            title=str((title_row.title if title_row is not None else row.title) or ""),
            year=title_row.year if title_row is not None else None,
            overview=str(title_row.overview or "") if title_row is not None else "",
            poster_url=str(title_row.poster_url or "") if title_row is not None else "",
            backdrop_url=str(title_row.backdrop_url or "") if title_row is not None else "",
            status=str(title_row.status or "") if title_row is not None else "",
            slug=str(title_row.slug or "") if title_row is not None else "",
            trakt_rating=title_row.trakt_rating if title_row is not None else None,
            trakt_votes=title_row.trakt_votes if title_row is not None else None,
            tmdb_id=title_row.tmdb_id if title_row is not None else None,
            tmdb_rating=title_row.tmdb_rating if title_row is not None else None,
            tmdb_votes=title_row.tmdb_votes if title_row is not None else None,
            imdb_id=str(title_row.imdb_id or "") if title_row is not None else "",
            imdb_rating=title_row.imdb_rating if title_row is not None else None,
            imdb_votes=title_row.imdb_votes if title_row is not None else None,
            ratings_status=(
                str(title_row.ratings_status or ENRICH_STATUS_UNKNOWN)
                if title_row is not None
                else ENRICH_STATUS_UNKNOWN
            ),
            ratings_refreshed_at=title_row.ratings_refreshed_at if title_row is not None else None,
            poster_refreshed_at=title_row.poster_refreshed_at if title_row is not None else None,
            backdrop_refreshed_at=title_row.backdrop_refreshed_at if title_row is not None else None,
            released_at=row.release_at,
            explore_metric_kind="lists",
            explore_metric_count=row.list_count,
            is_release_tracked=True,
            release_acknowledged=row.acknowledged_at is not None,
        )

    def refresh_anticipated_list_counts(self, items: list, *, force: bool = False, max_pages: int = 10) -> None:
        now = time.monotonic()
        with self._list_count_refresh_lock:
            if not force and now - self._list_count_refreshed_at < 21600:
                return
            self._list_count_refreshed_at = now
        client = self._auth.get_client()
        targets = {(item.title_type, int(item.trakt_id)) for item in items}
        found: dict[tuple[str, int], int] = {}
        for title_type in ("movie", "show"):
            remaining = {key for key in targets if key[0] == title_type}
            page = 1
            while remaining and page <= max(1, int(max_pages)):
                try:
                    result = client.get_explore_titles(title_type, "anticipated", page=page, limit=100)
                except Exception:
                    break
                for candidate in result.items:
                    key = (candidate.title_type, int(candidate.trakt_id))
                    if key in remaining and candidate.explore_metric_count is not None:
                        found[key] = int(candidate.explore_metric_count)
                        remaining.discard(key)
                if page >= result.page_count:
                    break
                page += 1
        if not found:
            return
        with self._db.session() as session:
            for (title_type, trakt_id), count in found.items():
                self._repository.set_list_count(session, title_type, trakt_id, count)

    def keys(self) -> set[tuple[str, int]]:
        return {(item.title_type, int(item.trakt_id)) for item in self.refresh()}

    def local_keys(self) -> set[tuple[str, int]]:
        with self._db.session() as session:
            return {(row.title_type, int(row.trakt_id)) for row in self._repository.list_all(session)}

    def set_tracked(self, title_type: str, trakt_id: int, *, tracked: bool, list_count: int | None = None) -> bool:
        self._auth.get_client().set_release_tracking(title_type, trakt_id, tracked=tracked)
        if not tracked:
            with self._db.session() as session:
                self._repository.delete(session, title_type, trakt_id)
        else:
            self.refresh()
            with self._db.session() as session:
                self._repository.set_list_count(session, title_type, trakt_id, list_count)
        self._notify_notification_state_changed()
        return tracked

    def set_acknowledged(self, title_type: str, trakt_id: int, *, acknowledged: bool) -> bool:
        with self._db.session() as session:
            result = self._repository.set_acknowledged(session, title_type, trakt_id, acknowledged)
        self._notify_notification_state_changed()
        return result

    def poll(self, *, send_native: bool = True) -> list[dict]:
        config = self._config_store.load()
        self.refresh()
        if not config.notifications_enabled:
            return []
        now = datetime.now(tz=UTC)
        repeat = timedelta(minutes=max(1, int(config.notification_repeat_minutes or 1)))
        sent: list[dict] = []
        with self._db.session() as session:
            for row in self._repository.list_all(session):
                release_at = self._as_utc(row.release_at)
                if release_at is None or release_at > now or row.acknowledged_at is not None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now < release_at + timedelta(minutes=max(0, delay_minutes)):
                    continue
                last_sent = self._as_utc(row.last_sent_at)
                if last_sent is not None and now - last_sent < repeat:
                    continue
                body = "Movie is now available" if row.title_type == "movie" else "Show has premiered"
                if send_native:
                    self._sender.send(NotificationMessage(title=row.title, body=body))
                self._repository.mark_sent(session, row.title_type, int(row.trakt_id))
                sent.append({"show_title": row.title, "message": body, "source": "release"})
        return sent

    def released_count(self) -> int:
        with self._db.session() as session:
            return self._repository.released_count(session)

    def has_due_unacknowledged_release(self) -> bool:
        now = datetime.now(tz=UTC)
        config = self._config_store.load()
        with self._db.session() as session:
            for row in self._repository.list_all(session):
                release_at = self._as_utc(row.release_at)
                if release_at is None or row.acknowledged_at is not None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now >= release_at + timedelta(minutes=max(0, delay_minutes)):
                    return True
            return False

    def progress_waiting_count(self) -> int:
        now = datetime.now(tz=UTC)
        config = self._config_store.load()
        with self._db.session() as session:
            items = self._progress_repository.list_in_progress(
                session,
                view=ProgressView.ACTIVE,
            )
            if bool(getattr(config, "hide_upcoming_in_progress", False)):
                items = [
                    item for item in items
                    if int(item.completed or 0) < max(
                        int(item.aired or 0),
                        int(item.completed or 0) + 1
                        if item.next_episode is not None
                        and item.next_episode.first_aired is not None
                        and self._known_utc(item.next_episode.first_aired) <= now
                        else 0,
                    )
                ]
            if bool(getattr(config, "web_progress_year_filter_enabled", False)) and getattr(config, "web_progress_min_year", None) is not None:
                minimum_year = int(config.web_progress_min_year)
                items = [
                    item for item in items
                    if item.next_episode is not None
                    and item.next_episode.first_aired is not None
                    and item.next_episode.first_aired.year >= minimum_year
                ]
            return sum(
                1
                for item in items
                if item.next_episode is not None
                and item.next_episode.first_aired is not None
                and self._known_utc(item.next_episode.first_aired) <= now
            )

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _known_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
