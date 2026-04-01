from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
        repeat_interval = timedelta(minutes=max(1, int(config.poll_interval_minutes or 1)))
        client = self._auth.get_client()
        now = datetime.now(tz=UTC)
        start_date = (now - timedelta(days=self._CALENDAR_LOOKBACK_DAYS)).date().isoformat()
        entries = client.get_calendar(start_date, days=self._CALENDAR_SPAN_DAYS)
        sent: list[dict] = []
        with self._db.session() as session:
            current_next_episode_ids = {
                item.trakt_id: item.next_episode.trakt_id
                for item in self._progress_repo.list_in_progress(session, dropped_only=False)
                if item.next_episode is not None
            }
            for entry in entries:
                if entry.episode.first_aired is None:
                    continue
                expected_episode_id = current_next_episode_ids.get(entry.show_trakt_id)
                if expected_episode_id != entry.episode.trakt_id:
                    self._notification_repo.delete_sent(session, entry.show_trakt_id, entry.episode.trakt_id)
                    continue
                release_at = entry.episode.first_aired.astimezone(UTC)
                sent_log = self._notification_repo.get_log(session, entry.show_trakt_id, entry.episode.trakt_id)
                if entry.episode.first_aired > now:
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
                if sent_log is not None and sent_log.seen_at is not None:
                    seen_at = sent_log.seen_at
                    if seen_at.tzinfo is None:
                        seen_at = seen_at.replace(tzinfo=UTC)
                    if seen_at >= release_at:
                        continue
                    self._notification_repo.delete_sent(session, entry.show_trakt_id, entry.episode.trakt_id)
                    sent_log = None
                if sent_log is not None and sent_log.last_sent_at is not None:
                    last_sent_at = sent_log.last_sent_at
                    if last_sent_at.tzinfo is None:
                        last_sent_at = last_sent_at.replace(tzinfo=UTC)
                    if now - last_sent_at < repeat_interval:
                        continue
                if send_native:
                    self._sender.send(NotificationMessage(title=entry.show_title, body=message))
                self._notification_repo.mark_sent(
                    session,
                    show_trakt_id=entry.show_trakt_id,
                    show_title=entry.show_title,
                    episode_trakt_id=entry.episode.trakt_id,
                    season=entry.episode.season,
                    episode=entry.episode.number,
                    message=message,
                )
                sent.append({"show_title": entry.show_title, "message": message})
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
