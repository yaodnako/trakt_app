from __future__ import annotations

from datetime import UTC, datetime

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_READY,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.domain import HistoryItemInput, RatingInput, TitleSummary


UNDATED_HISTORY_AT = datetime(1970, 1, 1, tzinfo=UTC)


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
        self.add_history_items([item])

    def add_history_items(self, items: list[HistoryItemInput]) -> None:
        if not items:
            return
        client = self._auth.get_client()
        remote_items: list[HistoryItemInput] = []
        with self._db.session() as session:
            hydrated_show_ids: set[int] = set()
            for item in items:
                watched_at_known = item.watched_at is not None
                local_watched_at = item.watched_at or UNDATED_HISTORY_AT
                existing_local = self._history.find_recent_local_watch(
                    session,
                    title_trakt_id=item.trakt_id,
                    season=item.season,
                    episode=item.episode,
                    watched_at=local_watched_at,
                    watched_at_known=watched_at_known,
                )
                remote_item = item
                if item.title_type == "show" and item.season is not None and item.episode is not None:
                    episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                    if episode_row is None and item.trakt_id not in hydrated_show_ids:
                        episodes = client.get_show_episodes(item.trakt_id)
                        self._episode_repo.replace_show_episodes(session, item.trakt_id, episodes)
                        hydrated_show_ids.add(item.trakt_id)
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
                if existing_local is None and watched_at_known:
                    remote_items.append(remote_item)
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
                if watched_at_known:
                    state.last_watched_at = local_watched_at
                self._history.add_event(
                    session,
                    trakt_history_id=None,
                    title_trakt_id=item.trakt_id,
                    title=title.title,
                    title_type=item.title_type,
                    action="watched",
                    watched_at=local_watched_at,
                    watched_at_known=watched_at_known,
                    season=item.season,
                    episode=item.episode,
                    source="local",
                )
            if remote_items:
                if hasattr(client, "add_history_items"):
                    client.add_history_items(remote_items)
                else:
                    for remote_item in remote_items:
                        client.add_history_item(remote_item)

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
        rated_only: bool = False,
    ) -> list[dict]:
        return self._history_read_model.history(
            title_type=title_type,
            limit=limit,
            offset=offset,
            title_filter=title_filter,
            rated_only=rated_only,
        )

    def history_title_summaries(
        self,
        title_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        title_filter: str | None = None,
        rated_only: bool = False,
    ) -> list[dict]:
        return self._history_read_model.history_title_summaries(
            title_type=title_type,
            limit=limit,
            offset=offset,
            title_filter=title_filter,
            rated_only=rated_only,
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

    def title_rating_badges(self, trakt_ids: list[int]) -> dict[int, float]:
        if not trakt_ids:
            return {}
        unique_ids = list(dict.fromkeys(int(item) for item in trakt_ids if item))
        with self._db.session() as session:
            title_ratings = self._user_states.ratings_by_trakt_ids(session, unique_ids)
            rated_map = self._history.latest_rated_map(session)
        show_totals: dict[int, tuple[float, int]] = {}
        for (trakt_id, season, episode), rating in rated_map.items():
            if trakt_id not in unique_ids or rating is None:
                continue
            if season is None or episode is None:
                title_ratings.setdefault(trakt_id, int(rating))
                continue
            total, count = show_totals.get(trakt_id, (0.0, 0))
            show_totals[trakt_id] = (total + float(rating), count + 1)
        badges: dict[int, float] = {trakt_id: float(rating) for trakt_id, rating in title_ratings.items()}
        for trakt_id, (total, count) in show_totals.items():
            if count:
                badges[trakt_id] = total / count
        return badges

    def has_missing_visible_episode_details(self, rows: list[dict]) -> bool:
        return bool(self._episode_metadata.select_episode_enrich_keys(rows))

    def select_episode_enrich_keys(
        self,
        rows: list[dict],
        *,
        trigger: str = "viewport",
        requested_parts=(),
        refresh_requests=None,
    ) -> list[tuple[int, int, int]]:
        return self._episode_metadata.select_episode_enrich_keys(
            rows,
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )

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
