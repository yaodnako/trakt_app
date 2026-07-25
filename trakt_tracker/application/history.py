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

    def remove_episode_watch(self, *, show_trakt_id: int, season: int, episode: int) -> dict:
        client = self._auth.get_client()
        with self._db.session() as session:
            episode_row = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
            if episode_row is None:
                episodes = client.get_show_episodes(show_trakt_id)
                self._episode_repo.replace_show_episodes(session, show_trakt_id, episodes)
                episode_row = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
            if episode_row is None or not episode_row.episode_trakt_id:
                raise RuntimeError("Episode metadata was not found for the selected season/episode")

            previous = self._history.episode_watch(
                session,
                show_trakt_id=show_trakt_id,
                season=season,
                episode=episode,
            )
            if previous is None:
                raise RuntimeError("This episode is not marked watched.")
            restore_watched_at = previous.watched_at
            restore_watched_at_known = bool(previous.watched_at_known)
            restore_title = previous.title

            client.remove_history_items(
                [
                    HistoryItemInput(
                        title_type="show",
                        trakt_id=int(episode_row.episode_trakt_id),
                        watched_at=None,
                        season=season,
                        episode=episode,
                        title=restore_title,
                    )
                ]
            )

            self._history.remove_episode_watch(
                session,
                show_trakt_id=show_trakt_id,
                season=season,
                episode=episode,
            )
            title = self._titles.get_title(session, show_trakt_id)
            if title is not None:
                state = self._user_states.ensure_state(session, title.id)
                latest = self._history.latest_watch_for_title(session, title_type="show", trakt_id=show_trakt_id)
                state.in_history = latest is not None
                state.last_watched_at = (
                    latest.watched_at
                    if latest is not None and bool(latest.watched_at_known)
                    else None
                )
                if latest is None:
                    state.tracked = False

        normalized_watched_at = restore_watched_at
        if normalized_watched_at.tzinfo is None:
            normalized_watched_at = normalized_watched_at.replace(tzinfo=UTC)
        return {
            "title_type": "show",
            "trakt_id": int(show_trakt_id),
            "title": restore_title,
            "season": int(season),
            "episode": int(episode),
            "watched_at": normalized_watched_at.astimezone(UTC).isoformat(),
            "watched_at_known": restore_watched_at_known,
        }

    def remove_watch_scope(
        self,
        *,
        title_type: str,
        trakt_id: int,
        scope: str,
        season: int | None = None,
    ) -> dict:
        normalized_type = "show" if title_type == "show" else "movie"
        if normalized_type == "movie":
            scope = "title"
            season = None
        elif scope not in {"season", "title"}:
            raise RuntimeError("Unsupported watched-history scope.")
        if scope == "season" and season is None:
            raise RuntimeError("Missing season identity.")

        client = self._auth.get_client()
        with self._db.session() as session:
            rows = self._history.watches_for_scope(
                session,
                title_type=normalized_type,
                trakt_id=trakt_id,
                season=season if scope == "season" else None,
            )
            if not rows:
                raise RuntimeError("This title is not marked watched.")

            remote_items: list[HistoryItemInput] = []
            if normalized_type == "movie":
                remote_items.append(
                    HistoryItemInput(
                        title_type="movie",
                        trakt_id=trakt_id,
                        watched_at=None,
                        title=rows[0].title,
                    )
                )
            else:
                keys = {
                    (int(row.season), int(row.episode))
                    for row in rows
                    if row.season is not None and row.episode is not None
                }
                episode_rows = {
                    key: self._episode_repo.find_episode(session, trakt_id, key[0], key[1])
                    for key in keys
                }
                if any(row is None or not row.episode_trakt_id for row in episode_rows.values()):
                    episodes = client.get_show_episodes(trakt_id)
                    self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
                    episode_rows = {
                        key: self._episode_repo.find_episode(session, trakt_id, key[0], key[1])
                        for key in keys
                    }
                if any(row is None or not row.episode_trakt_id for row in episode_rows.values()):
                    raise RuntimeError("Episode metadata was not found for all watched episodes.")
                remote_items.extend(
                    HistoryItemInput(
                        title_type="show",
                        trakt_id=int(episode_rows[key].episode_trakt_id),
                        watched_at=None,
                        season=key[0],
                        episode=key[1],
                        title=rows[0].title,
                    )
                    for key in sorted(keys)
                )

            client.remove_history_items(remote_items)
            removed = self._history.remove_watches_for_scope(
                session,
                title_type=normalized_type,
                trakt_id=trakt_id,
                season=season if scope == "season" else None,
            )
            title = self._titles.get_title(session, trakt_id)
            if title is not None:
                state = self._user_states.ensure_state(session, title.id)
                latest = self._history.latest_watch_for_title(
                    session,
                    title_type=normalized_type,
                    trakt_id=trakt_id,
                )
                state.in_history = latest is not None
                state.last_watched_at = (
                    latest.watched_at
                    if latest is not None and bool(latest.watched_at_known)
                    else None
                )
                if normalized_type == "show" and latest is None:
                    state.tracked = False
            still_watched = self._history.latest_watch_for_title(
                session,
                title_type=normalized_type,
                trakt_id=trakt_id,
            ) is not None

        return {
            "kind": "scope",
            "title_type": normalized_type,
            "trakt_id": int(trakt_id),
            "title": removed[0].title,
            "scope": scope,
            "season": season,
            "still_watched": still_watched,
            "items": [self._watch_restore_item(row) for row in removed],
        }

    def restore_watch_scope(self, *, items: list[dict]) -> None:
        if not items:
            raise RuntimeError("Missing watched-history restore data.")
        client = self._auth.get_client()
        with self._db.session() as session:
            remote_items: list[HistoryItemInput] = []
            episode_remote_ids: dict[tuple[int, int, int], int] = {}
            show_ids = {
                int(item["trakt_id"])
                for item in items
                if item.get("title_type") == "show"
            }
            for show_id in show_ids:
                keys = {
                    (show_id, int(item["season"]), int(item["episode"]))
                    for item in items
                    if item.get("title_type") == "show" and int(item["trakt_id"]) == show_id
                }
                missing = False
                for key in keys:
                    row = self._episode_repo.find_episode(session, key[0], key[1], key[2])
                    if row is None or not row.episode_trakt_id:
                        missing = True
                    else:
                        episode_remote_ids[key] = int(row.episode_trakt_id)
                if missing:
                    episodes = client.get_show_episodes(show_id)
                    self._episode_repo.replace_show_episodes(session, show_id, episodes)
                    for key in keys:
                        row = self._episode_repo.find_episode(session, key[0], key[1], key[2])
                        if row is None or not row.episode_trakt_id:
                            raise RuntimeError("Episode metadata was not found for all watched episodes.")
                        episode_remote_ids[key] = int(row.episode_trakt_id)

            for item in items:
                title_type = "show" if item.get("title_type") == "show" else "movie"
                parent_trakt_id = int(item["trakt_id"])
                watched_at = item["watched_at"]
                watched_at_known = bool(item.get("watched_at_known", True))
                season_number = int(item["season"]) if item.get("season") is not None else None
                episode_number = int(item["episode"]) if item.get("episode") is not None else None
                remote_trakt_id = parent_trakt_id
                if title_type == "show":
                    remote_trakt_id = episode_remote_ids[(parent_trakt_id, season_number, episode_number)]
                remote_items.append(
                    HistoryItemInput(
                        title_type=title_type,
                        trakt_id=remote_trakt_id,
                        watched_at=watched_at if watched_at_known else UNDATED_HISTORY_AT,
                        season=season_number,
                        episode=episode_number,
                        title=str(item.get("title") or ""),
                    )
                )

            client.add_history_items(remote_items)
            latest_by_title: dict[tuple[str, int], datetime] = {}
            for item in items:
                title_type = "show" if item.get("title_type") == "show" else "movie"
                trakt_id = int(item["trakt_id"])
                watched_at = item["watched_at"]
                watched_at_known = bool(item.get("watched_at_known", True))
                title_text = str(item.get("title") or f"{title_type.capitalize()} {trakt_id}")
                title = self._titles.get_title(session, trakt_id)
                if title is None:
                    title = self._titles.upsert_title(
                        session,
                        TitleSummary(trakt_id=trakt_id, title_type=title_type, title=title_text),
                    )
                state = self._user_states.ensure_state(session, title.id)
                state.in_history = True
                state.tracked = title_type == "show"
                if watched_at_known:
                    key = (title_type, trakt_id)
                    latest_by_title[key] = max(latest_by_title.get(key, watched_at), watched_at)
                self._history.add_event(
                    session,
                    trakt_history_id=None,
                    title_trakt_id=trakt_id,
                    title=title.title,
                    title_type=title_type,
                    action="watched",
                    watched_at=watched_at,
                    watched_at_known=watched_at_known,
                    season=int(item["season"]) if item.get("season") is not None else None,
                    episode=int(item["episode"]) if item.get("episode") is not None else None,
                    source="local",
                )
            for (title_type, trakt_id), watched_at in latest_by_title.items():
                title = self._titles.get_title(session, trakt_id)
                if title is not None:
                    self._user_states.ensure_state(session, title.id).last_watched_at = watched_at

    @staticmethod
    def _watch_restore_item(row) -> dict:
        watched_at = row.watched_at
        if watched_at.tzinfo is None:
            watched_at = watched_at.replace(tzinfo=UTC)
        return {
            "title_type": row.title_type,
            "trakt_id": int(row.title_trakt_id),
            "title": row.title,
            "season": row.season,
            "episode": row.episode,
            "watched_at": watched_at.astimezone(UTC).isoformat(),
            "watched_at_known": bool(row.watched_at_known),
        }

    def restore_episode_watch(
        self,
        *,
        show_trakt_id: int,
        title: str,
        season: int,
        episode: int,
        watched_at: datetime,
        watched_at_known: bool,
    ) -> None:
        if watched_at_known:
            self.add_history_item(
                HistoryItemInput(
                    title_type="show",
                    trakt_id=show_trakt_id,
                    watched_at=watched_at,
                    season=season,
                    episode=episode,
                    title=title,
                )
            )
            return

        client = self._auth.get_client()
        with self._db.session() as session:
            episode_row = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
            if episode_row is None or not episode_row.episode_trakt_id:
                raise RuntimeError("Episode metadata was not found for the selected season/episode")
            client.add_history_items(
                [
                    HistoryItemInput(
                        title_type="show",
                        trakt_id=int(episode_row.episode_trakt_id),
                        watched_at=UNDATED_HISTORY_AT,
                        season=season,
                        episode=episode,
                        title=title,
                    )
                ]
            )
            title_row = self._titles.get_title(session, show_trakt_id)
            if title_row is not None:
                state = self._user_states.ensure_state(session, title_row.id)
                state.in_history = True
                state.tracked = True
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=show_trakt_id,
                title=title,
                title_type="show",
                action="watched",
                watched_at=UNDATED_HISTORY_AT,
                watched_at_known=False,
                season=season,
                episode=episode,
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
        sort_by: str = "last_watched",
        sort_direction: str = "desc",
    ) -> list[dict]:
        return self._history_read_model.history_title_summaries(
            title_type=title_type,
            limit=limit,
            offset=offset,
            title_filter=title_filter,
            rated_only=rated_only,
            sort_by=sort_by,
            sort_direction=sort_direction,
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
