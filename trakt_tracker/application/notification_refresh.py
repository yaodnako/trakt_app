from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trakt_tracker.domain import ProgressView
from trakt_tracker.infrastructure.notifications import NotificationMessage


class NotificationRefreshWorkflow:
    _CALENDAR_LOOKBACK_DAYS = 1
    _CALENDAR_SPAN_DAYS = 15

    def __init__(
        self,
        db,
        auth_service,
        config_store,
        notification_repo,
        episode_repo,
        progress_repo,
        sender,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._config_store = config_store
        self._notification_repo = notification_repo
        self._episode_repo = episode_repo
        self._progress_repo = progress_repo
        self._sender = sender

    def poll_upcoming(self, *, send_native: bool = True) -> list[dict]:
        config = self._config_store.load()
        if not config.notifications_enabled:
            return []
        repeat_interval = timedelta(minutes=max(1, int(config.notification_repeat_minutes or 1)))
        release_delay = timedelta(minutes=max(0, int(getattr(config, "notification_release_delay_minutes", 120) or 0)))
        client = self._auth.get_client()
        now = datetime.now(tz=UTC)
        start_date = (now - timedelta(days=self._CALENDAR_LOOKBACK_DAYS)).date().isoformat()
        entries = client.get_calendar(start_date, days=self._CALENDAR_SPAN_DAYS)
        sent: list[dict] = []
        with self._db.session() as session:
            active_items = self._progress_repo.list_in_progress(
                session,
                view=ProgressView.ACTIVE,
                limit=None,
            )
            paused_items = self._progress_repo.list_in_progress(
                session,
                view=ProgressView.PAUSED,
                limit=None,
            )
            current_next_episodes = {
                item.trakt_id: item.next_episode
                for item in [*active_items, *paused_items]
                if item.next_episode is not None
            }
            paused_show_ids = {int(item.trakt_id) for item in paused_items}
            for entry in entries:
                expected_episode = current_next_episodes.get(entry.show_trakt_id)
                if expected_episode is None or expected_episode.trakt_id != entry.episode.trakt_id:
                    self._notification_repo.delete_sent(session, entry.show_trakt_id, entry.episode.trakt_id)
                    continue
                first_aired = entry.episode.first_aired or expected_episode.first_aired
                if first_aired is None:
                    continue
                release_at = first_aired.replace(tzinfo=UTC) if first_aired.tzinfo is None else first_aired.astimezone(UTC)
                sent_log = self._notification_repo.get_log(session, entry.show_trakt_id, entry.episode.trakt_id)
                if release_at > now:
                    if sent_log is not None:
                        self._notification_repo.delete_sent(session, entry.show_trakt_id, entry.episode.trakt_id)
                    continue
                if sent_log is not None:
                    sent_at = sent_log.sent_at
                    if sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=UTC)
                    if sent_at < release_at:
                        self._notification_repo.delete_sent(session, entry.show_trakt_id, entry.episode.trakt_id)
                        sent_log = None
                message = f"S{entry.episode.season:02d}E{entry.episode.number:02d} {entry.episode.title}"
                if sent_log is None:
                    self._notification_repo.track_released(
                        session,
                        show_trakt_id=entry.show_trakt_id,
                        show_title=entry.show_title,
                        episode_trakt_id=entry.episode.trakt_id,
                        season=entry.episode.season,
                        episode=entry.episode.number,
                        message=message,
                        released_at=release_at,
                    )
                    sent_log = self._notification_repo.get_log(session, entry.show_trakt_id, entry.episode.trakt_id)
                if int(entry.show_trakt_id) in paused_show_ids:
                    self._notification_repo.mark_seen(
                        session,
                        show_trakt_id=entry.show_trakt_id,
                        show_title=entry.show_title,
                        episode_trakt_id=entry.episode.trakt_id,
                        season=entry.episode.season,
                        episode=entry.episode.number,
                        message=message,
                    )
                    continue
                if now < release_at + release_delay:
                    continue
                if sent_log is not None and sent_log.seen_at is not None:
                    seen_at = sent_log.seen_at
                    if seen_at.tzinfo is None:
                        seen_at = seen_at.replace(tzinfo=UTC)
                    if seen_at >= release_at:
                        continue
                    self._notification_repo.delete_sent(session, entry.show_trakt_id, entry.episode.trakt_id)
                    self._notification_repo.track_released(
                        session,
                        show_trakt_id=entry.show_trakt_id,
                        show_title=entry.show_title,
                        episode_trakt_id=entry.episode.trakt_id,
                        season=entry.episode.season,
                        episode=entry.episode.number,
                        message=message,
                        released_at=release_at,
                    )

            for sent_log in self._notification_repo.list_unseen(session):
                expected_episode = current_next_episodes.get(sent_log.show_trakt_id)
                if expected_episode is None or expected_episode.trakt_id != sent_log.episode_trakt_id:
                    self._notification_repo.delete_sent(
                        session,
                        sent_log.show_trakt_id,
                        sent_log.episode_trakt_id,
                    )
                    continue
                first_aired = expected_episode.first_aired or sent_log.sent_at
                release_at = first_aired.replace(tzinfo=UTC) if first_aired.tzinfo is None else first_aired.astimezone(UTC)
                if release_at > now:
                    self._notification_repo.delete_sent(
                        session,
                        sent_log.show_trakt_id,
                        sent_log.episode_trakt_id,
                    )
                    continue
                if int(sent_log.show_trakt_id) in paused_show_ids:
                    self._notification_repo.mark_seen(
                        session,
                        show_trakt_id=sent_log.show_trakt_id,
                        show_title=sent_log.show_title,
                        episode_trakt_id=sent_log.episode_trakt_id,
                        season=sent_log.season,
                        episode=sent_log.episode,
                        message=sent_log.message,
                    )
                    continue
                if now < release_at + release_delay:
                    continue
                last_sent_at = sent_log.last_sent_at
                if last_sent_at is not None:
                    last_sent_at = last_sent_at.replace(tzinfo=UTC) if last_sent_at.tzinfo is None else last_sent_at.astimezone(UTC)
                    if now - last_sent_at < repeat_interval:
                        continue
                if send_native:
                    self._sender.send(NotificationMessage(title=sent_log.show_title, body=sent_log.message))
                self._notification_repo.mark_sent(
                    session,
                    show_trakt_id=sent_log.show_trakt_id,
                    show_title=sent_log.show_title,
                    episode_trakt_id=sent_log.episode_trakt_id,
                    season=sent_log.season,
                    episode=sent_log.episode,
                    message=sent_log.message,
                )
                sent.append({"show_title": sent_log.show_title, "message": sent_log.message, "source": "progress"})
        return sent

    def mark_episode_seen(self, *, show_trakt_id: int, show_title: str, episode) -> None:
        message = f"S{episode.season:02d}E{episode.number:02d} {episode.title}"
        with self._db.session() as session:
            self._notification_repo.mark_seen(
                session,
                show_trakt_id=show_trakt_id,
                show_title=show_title,
                episode_trakt_id=episode.trakt_id,
                season=episode.season,
                episode=episode.number,
                message=message,
            )

    def unseen_episode_ids(self) -> set[int]:
        with self._db.session() as session:
            return self._notification_repo.unseen_episode_ids(session)

    def has_due_unseen_current_episode(self) -> bool:
        config = self._config_store.load()
        release_delay = timedelta(
            minutes=max(0, int(getattr(config, "notification_release_delay_minutes", 120) or 0))
        )
        now = datetime.now(tz=UTC)
        with self._db.session() as session:
            current_episodes = {
                int(item.next_episode.trakt_id): item.next_episode
                for item in self._progress_repo.list_in_progress(
                    session,
                    view=ProgressView.ACTIVE,
                    limit=None,
                )
                if item.next_episode is not None
            }
            for row in self._notification_repo.list_unseen(session):
                episode = current_episodes.get(int(row.episode_trakt_id))
                if episode is None:
                    continue
                first_aired = episode.first_aired or row.sent_at
                release_at = (
                    first_aired.replace(tzinfo=UTC)
                    if first_aired.tzinfo is None
                    else first_aired.astimezone(UTC)
                )
                if now >= release_at + release_delay:
                    return True
            return False

    def upcoming_items(self) -> list[dict]:
        with self._db.session() as session:
            rows = self._episode_repo.list_upcoming(session)
            return [
                {
                    "show_title": row.show_title,
                    "episode_trakt_id": row.episode.trakt_id,
                    "episode_title": row.episode.title,
                    "season": row.episode.season,
                    "episode": row.episode.number,
                    "first_aired": row.episode.first_aired,
                }
                for row in rows
            ]
