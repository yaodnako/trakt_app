from __future__ import annotations


class HistoryReadModelService:
    def __init__(self, db, history_repo, user_states, titles_repo, episode_repo, episode_metadata) -> None:
        self._db = db
        self._history = history_repo
        self._user_states = user_states
        self._titles = titles_repo
        self._episode_repo = episode_repo
        self._episode_metadata = episode_metadata

    def history(
        self,
        *,
        title_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        title_filter: str | None = None,
    ) -> list[dict]:
        with self._db.session() as session:
            rows = self._history.list_filtered(
                session,
                title_type=title_type,
                title_filter=title_filter,
                action="watched",
            )
            rows = self._dedupe_history_rows(rows)
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            title_models = {
                row.title_trakt_id: self._titles.get_title(session, row.title_trakt_id)
                for row in rows
            }
            ratings = self._user_states.ratings_by_trakt_ids(session, [row.title_trakt_id for row in rows])
            rated_map = self._history.latest_rated_map(
                session,
                title_type=title_type,
                title_filter=title_filter,
            )
            episode_metadata = self._episode_repo.metadata_by_episode_keys(
                session,
                [
                    (row.title_trakt_id, row.season, row.episode)
                    for row in rows
                    if row.title_type == "show" and row.season is not None and row.episode is not None
                ],
            )
            cached_episode_imdb = self._episode_metadata.load_cached_episode_imdb_metadata(
                [
                    (row.title_trakt_id, row.season, row.episode)
                    for row in rows
                    if row.title_type == "show" and row.season is not None and row.episode is not None
                ]
            )
            cached_title_ratings, cached_episode_ratings = self._episode_metadata.load_cached_trakt_rating_maps()
            return [
                {
                    "title_trakt_id": row.title_trakt_id,
                    "title": row.title,
                    "poster_url": (title_models.get(row.title_trakt_id).poster_url if title_models.get(row.title_trakt_id) is not None else ""),
                    "type": row.title_type,
                    "action": row.action,
                    "watched_at": row.watched_at,
                    "season": row.season,
                    "episode": row.episode,
                    "episode_title": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("title")
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "episode_still_url": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("still_url")
                        if row.season is not None and row.episode is not None
                        else ""
                    ),
                    "episode_trakt_rating": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("trakt_rating")
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "episode_trakt_votes": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("trakt_votes")
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "episode_imdb_rating": (
                        (
                            (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_rating")
                            or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_rating")
                        )
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "episode_imdb_votes": (
                        (
                            (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_votes")
                            or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_votes")
                        )
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "event_rating": row.rating,
                    "title_rating": ratings.get(row.title_trakt_id),
                    "display_rating": (
                        row.rating
                        or rated_map.get((row.title_trakt_id, row.season, row.episode))
                        or cached_episode_ratings.get((row.title_trakt_id, row.season, row.episode))
                        if row.title_type == "show"
                        else (
                            row.rating
                            or rated_map.get((row.title_trakt_id, None, None), ratings.get(row.title_trakt_id))
                            or cached_title_ratings.get(row.title_trakt_id)
                        )
                    ),
                }
                for row in rows
            ]

    @staticmethod
    def _dedupe_history_rows(rows: list) -> list:
        seen: set[tuple[str, int, int | None, int | None]] = set()
        deduped: list = []
        for row in rows:
            key = (row.title_type, row.title_trakt_id, row.season, row.episode)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped
