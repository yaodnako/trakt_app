from __future__ import annotations

from datetime import UTC, datetime

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
    should_attempt_enrich,
)
from trakt_tracker.domain import HistoryItemInput, RatingInput, TitleSummary


class HistoryService:
    def __init__(
        self,
        db,
        auth_service,
        titles,
        user_states,
        history,
        episode_repo,
        history_read_model,
        episode_metadata,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles
        self._user_states = user_states
        self._history = history
        self._episode_repo = episode_repo
        self._history_read_model = history_read_model
        self._episode_metadata = episode_metadata

    def add_history_item(self, item: HistoryItemInput) -> None:
        client = self._auth.get_client()
        with self._db.session() as session:
            existing_local = self._history.find_recent_local_watch(
                session,
                title_trakt_id=item.trakt_id,
                season=item.season,
                episode=item.episode,
                watched_at=item.watched_at,
            )
            remote_item = item
            if item.title_type == "show" and item.season is not None and item.episode is not None:
                episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None:
                    episodes = client.get_show_episodes(item.trakt_id)
                    self._episode_repo.replace_show_episodes(session, item.trakt_id, episodes)
                    episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None or not episode_row.episode_trakt_id:
                    raise RuntimeError("Episode metadata was not found for the selected season/episode")
                remote_item = HistoryItemInput(
                    title_type=item.title_type,
                    trakt_id=episode_row.episode_trakt_id,
                    watched_at=item.watched_at,
                    season=item.season,
                    episode=item.episode,
                    title=item.title,
                )
            if existing_local is None:
                client.add_history_item(remote_item)
            title = self._titles.get_title(session, item.trakt_id)
            if title is None:
                title = self._titles.upsert_title(
                    session,
                    TitleSummary(
                        trakt_id=item.trakt_id,
                        title_type=item.title_type,
                        title=item.title or f"{item.title_type.capitalize()} {item.trakt_id}",
                    ),
                )
            state = self._user_states.ensure_state(session, title.id)
            state.in_history = True
            state.tracked = item.title_type == "show"
            state.last_watched_at = item.watched_at
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=item.trakt_id,
                title=title.title,
                title_type=item.title_type,
                action="watched",
                watched_at=item.watched_at,
                season=item.season,
                episode=item.episode,
                source="local",
            )

    def set_rating(self, item: RatingInput, title: str = "") -> None:
        client = self._auth.get_client()
        with self._db.session() as session:
            remote_item = item
            if item.title_type == "show" and item.season is not None and item.episode is not None:
                episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None:
                    episodes = client.get_show_episodes(item.trakt_id)
                    self._episode_repo.replace_show_episodes(session, item.trakt_id, episodes)
                    episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None or not episode_row.episode_trakt_id:
                    raise RuntimeError("Episode metadata was not found for the selected season/episode")
                remote_item = RatingInput(
                    title_type=item.title_type,
                    trakt_id=episode_row.episode_trakt_id,
                    rating=item.rating,
                    season=item.season,
                    episode=item.episode,
                )
            client.set_rating(remote_item)
            model = self._titles.get_title(session, item.trakt_id)
            if model is None:
                model = self._titles.upsert_title(
                    session,
                    TitleSummary(
                        trakt_id=item.trakt_id,
                        title_type=item.title_type,
                        title=title or f"{item.title_type.capitalize()} {item.trakt_id}",
                    ),
                )
            state = self._user_states.ensure_state(session, model.id)
            state.rating = item.rating
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=item.trakt_id,
                title=model.title,
                title_type=item.title_type,
                action="rated",
                watched_at=datetime.now(tz=UTC),
                season=item.season,
                episode=item.episode,
                rating=item.rating,
                source="local",
            )
            self._history.apply_rating_to_latest_watch(
                session,
                title_trakt_id=item.trakt_id,
                title_type=item.title_type,
                season=item.season,
                episode=item.episode,
                rating=item.rating,
            )

    def history(
        self,
        title_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        title_filter: str | None = None,
    ) -> list[dict]:
        return self._history_read_model.history(
            title_type=title_type,
            limit=limit,
            offset=offset,
            title_filter=title_filter,
        )

    def history_titles(self, title_type: str | None = None) -> list[str]:
        with self._db.session() as session:
            return self._history.distinct_titles(session, title_type=title_type, action="watched")

    def displayed_history_rating(
        self,
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> int | None:
        rows = self.history(title_type=title_type)
        for row in rows:
            if row["title_trakt_id"] != trakt_id:
                continue
            if row["season"] != season or row["episode"] != episode:
                continue
            return row.get("display_rating")
        return None

    def has_missing_visible_episode_details(self, rows: list[dict]) -> bool:
        episode_keys = [
            (int(row["title_trakt_id"]), int(row["season"]), int(row["episode"]))
            for row in rows
            if row.get("type") == "show" and row.get("season") is not None and row.get("episode") is not None
        ]
        if not episode_keys:
            return False
        with self._db.session() as session:
            metadata = self._episode_repo.metadata_by_episode_keys(session, episode_keys)
        for key in dict.fromkeys(episode_keys):
            item = metadata.get(key) or {}
            if should_attempt_enrich(
                item.get("trakt_details_status", ENRICH_STATUS_UNKNOWN),
                has_value=item.get("trakt_rating") is not None and item.get("trakt_votes") is not None,
            ):
                return True
            if self._episode_metadata.can_enrich_episode_stills() and should_attempt_enrich(
                item.get("still_status", ENRICH_STATUS_UNKNOWN),
                has_value=bool(item.get("still_url")),
            ):
                return True
        return False

    def enrich_visible_episode_details(self, rows: list[dict]) -> bool:
        episode_keys = [
            (int(row["title_trakt_id"]), int(row["season"]), int(row["episode"]))
            for row in rows
            if row.get("type") == "show" and row.get("season") is not None and row.get("episode") is not None
        ]
        if not episode_keys:
            return False
        with self._db.session() as session:
            metadata = self._episode_repo.metadata_by_episode_keys(session, episode_keys)
        missing_keys = [
            key
            for key in dict.fromkeys(episode_keys)
            if should_attempt_enrich(
                (metadata.get(key) or {}).get("trakt_details_status", ENRICH_STATUS_UNKNOWN),
                has_value=(
                    (metadata.get(key) or {}).get("trakt_rating") is not None
                    and (metadata.get(key) or {}).get("trakt_votes") is not None
                ),
            )
        ]
        changed = False
        if missing_keys:
            client = self._auth.get_client()
            with self._db.session() as session:
                for show_trakt_id, season, episode in missing_keys:
                    try:
                        details = client.get_episode_details(show_trakt_id, season, episode)
                    except Exception:
                        self._episode_repo.update_trakt_details_enrich_state(
                            session,
                            show_trakt_id,
                            season,
                            episode,
                            status=ENRICH_STATUS_RETRYABLE_FAILURE,
                        )
                        continue
                    if details is None:
                        self._episode_repo.update_trakt_details_enrich_state(
                            session,
                            show_trakt_id,
                            season,
                            episode,
                            status=ENRICH_STATUS_CHECKED_NO_DATA,
                        )
                        continue
                    existing = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
                    previous_rating = existing.trakt_rating if existing is not None else None
                    previous_votes = existing.trakt_votes if existing is not None else None
                    status = (
                        ENRICH_STATUS_CHECKED_NO_DATA
                        if details.trakt_rating is None or details.trakt_votes is None
                        else ENRICH_STATUS_READY
                    )
                    self._episode_repo.update_trakt_details_enrich_state(
                        session,
                        show_trakt_id,
                        season,
                        episode,
                        status=status,
                        details=details,
                    )
                    if details.trakt_rating != previous_rating or details.trakt_votes != previous_votes:
                        changed = True
        try:
            still_changed = self._episode_metadata.enrich_episode_stills(episode_keys)
        except Exception:
            still_changed = False
        return changed or still_changed
