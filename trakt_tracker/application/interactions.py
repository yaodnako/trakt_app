from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trakt_tracker.domain import HistoryItemInput, ProgressSnapshot, RatingInput


@dataclass(slots=True)
class EpisodeActionResult:
    title: str
    trakt_id: int
    season: int
    episode: int


class InteractionService:
    def __init__(self, history_service, notification_service, progress_service) -> None:
        self._history = history_service
        self._notifications = notification_service
        self._progress = progress_service

    def add_history_item(self, item: HistoryItemInput) -> None:
        self._history.add_history_item(item)

    def save_rating(self, item: RatingInput, *, title: str = "") -> None:
        self._history.set_rating(item, title=title)
        saved_rating = self._history.displayed_history_rating(
            title_type=item.title_type,
            trakt_id=item.trakt_id,
            season=item.season,
            episode=item.episode,
        )
        if saved_rating != item.rating:
            raise RuntimeError("Rating did not appear in history after save")

    def mark_progress_episode_watched(
        self,
        progress: ProgressSnapshot,
        *,
        watched_at: datetime | None = None,
    ) -> EpisodeActionResult:
        episode = progress.next_episode
        if episode is None:
            raise RuntimeError("No next episode to mark watched")
        self._notifications.mark_episode_seen(
            show_trakt_id=progress.trakt_id,
            show_title=progress.title,
            episode=episode,
        )
        self.add_history_item(
            HistoryItemInput(
                title_type="show",
                trakt_id=progress.trakt_id,
                watched_at=watched_at or datetime.now(),
                season=episode.season,
                episode=episode.number,
                title=progress.title,
            )
        )
        return EpisodeActionResult(
            title=progress.title,
            trakt_id=progress.trakt_id,
            season=episode.season,
            episode=episode.number,
        )

    def mark_progress_episode_seen(
        self,
        progress: ProgressSnapshot,
        *,
        now: datetime | None = None,
    ) -> EpisodeActionResult:
        episode = progress.next_episode
        if episode is None:
            raise RuntimeError("No released episode to mark seen")
        if episode.first_aired is None:
            raise RuntimeError("Episode has not aired yet")
        release_at = episode.first_aired
        if release_at.tzinfo is None:
            release_at = release_at.replace(tzinfo=UTC)
        current_time = now or datetime.now(tz=UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        if release_at > current_time:
            raise RuntimeError("Episode has not aired yet")
        self._notifications.mark_episode_seen(
            show_trakt_id=progress.trakt_id,
            show_title=progress.title,
            episode=episode,
        )
        return EpisodeActionResult(
            title=progress.title,
            trakt_id=progress.trakt_id,
            season=episode.season,
            episode=episode.number,
        )

    def set_progress_dropped(self, trakt_id: int, *, dropped: bool) -> None:
        if dropped:
            self._progress.drop_show(trakt_id)
        else:
            self._progress.undrop_show(trakt_id)
