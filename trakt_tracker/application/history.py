from __future__ import annotations

from datetime import UTC, datetime

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_READY,
    ENRICH_STATUS_UNKNOWN,
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
            if item.title_type == "show" and item.season is not None and item.episode is not None:
                episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is not None:
                    episode_row.trakt_details_status = ENRICH_STATUS_UNKNOWN
            else:
                model.ratings_status = ENRICH_STATUS_UNKNOWN

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
        return bool(self._episode_metadata.select_episode_enrich_keys(rows))

    def select_episode_enrich_keys(self, rows: list[dict]) -> list[tuple[int, int, int]]:
        return self._episode_metadata.select_episode_enrich_keys(rows)

    def episode_key_needs_enrich(self, show_trakt_id: int, season: int, episode: int) -> bool:
        return self._episode_metadata.episode_key_needs_enrich(show_trakt_id, season, episode)

    def enrich_visible_episode_details(self, rows: list[dict]) -> bool:
        episode_keys = self.select_episode_enrich_keys(rows)
        if not episode_keys:
            return False
        changed = False
        for show_trakt_id, season, episode in episode_keys:
            result = self.enrich_episode_key(show_trakt_id, season, episode)
            if result == ENRICH_STATUS_READY:
                changed = True
        return changed

    def enrich_episode_key(self, show_trakt_id: int, season: int, episode: int) -> str:
        return self._episode_metadata.enrich_episode_key(show_trakt_id, season, episode)
