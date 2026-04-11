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
        rated_only: bool = False,
    ) -> list[dict]:
        with self._db.session() as session:
            all_rows = self._history.list_filtered(
                session,
                title_type=title_type,
                title_filter=title_filter,
                action="watched",
            )
            all_rows = self._dedupe_history_rows(all_rows)
            rated_map = self._history.latest_rated_map(
                session,
                title_type=title_type,
                title_filter=title_filter,
            )
            cached_title_ratings, cached_episode_ratings = self._episode_metadata.load_cached_trakt_rating_maps()
            display_ratings_by_row_id = {
                int(row.id): self._display_rating_for_row(
                    row=row,
                    rated_map=rated_map,
                    cached_title_ratings=cached_title_ratings,
                    cached_episode_ratings=cached_episode_ratings,
                )
                for row in all_rows
            }
            if rated_only:
                all_rows = [
                    row
                    for row in all_rows
                    if row.title_type == "show"
                    and row.season is not None
                    and row.episode is not None
                    and display_ratings_by_row_id.get(int(row.id)) is not None
                ]
            title_episode_stats = self._build_title_episode_rating_stats(
                all_rows,
                rated_map=rated_map,
                cached_episode_ratings=cached_episode_ratings,
            )
            rows = all_rows
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            title_models = {
                row.title_trakt_id: self._titles.get_title(session, row.title_trakt_id)
                for row in rows
            }
            ratings = self._user_states.ratings_by_trakt_ids(session, [row.title_trakt_id for row in rows])
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
            return [
                {
                    "title_trakt_id": row.title_trakt_id,
                    "title": row.title,
                    "title_slug": (title_models.get(row.title_trakt_id).slug if title_models.get(row.title_trakt_id) is not None else ""),
                    "poster_url": (title_models.get(row.title_trakt_id).poster_url if title_models.get(row.title_trakt_id) is not None else ""),
                    "title_poster_status": (title_models.get(row.title_trakt_id).poster_status if title_models.get(row.title_trakt_id) is not None else "unknown"),
                    "title_poster_refreshed_at": (
                        title_models.get(row.title_trakt_id).poster_refreshed_at if title_models.get(row.title_trakt_id) is not None else None
                    ),
                    "backdrop_url": (title_models.get(row.title_trakt_id).backdrop_url if title_models.get(row.title_trakt_id) is not None else ""),
                    "title_backdrop_status": (title_models.get(row.title_trakt_id).backdrop_status if title_models.get(row.title_trakt_id) is not None else "unknown"),
                    "title_backdrop_refreshed_at": (
                        title_models.get(row.title_trakt_id).backdrop_refreshed_at if title_models.get(row.title_trakt_id) is not None else None
                    ),
                    "title_trakt_rating": (title_models.get(row.title_trakt_id).trakt_rating if title_models.get(row.title_trakt_id) is not None else None),
                    "title_trakt_votes": (title_models.get(row.title_trakt_id).trakt_votes if title_models.get(row.title_trakt_id) is not None else None),
                    "title_imdb_rating": (title_models.get(row.title_trakt_id).imdb_rating if title_models.get(row.title_trakt_id) is not None else None),
                    "title_imdb_votes": (title_models.get(row.title_trakt_id).imdb_votes if title_models.get(row.title_trakt_id) is not None else None),
                    "title_ratings_status": (title_models.get(row.title_trakt_id).ratings_status if title_models.get(row.title_trakt_id) is not None else "unknown"),
                    "title_ratings_refreshed_at": (title_models.get(row.title_trakt_id).ratings_refreshed_at if title_models.get(row.title_trakt_id) is not None else None),
                    "title_episode_avg_rating": (title_episode_stats.get(row.title_trakt_id) or {}).get("avg_rating"),
                    "title_episode_rated_count": (title_episode_stats.get(row.title_trakt_id) or {}).get("rated_count", 0),
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
                    "episode_still_status": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("still_status", "unknown")
                        if row.season is not None and row.episode is not None
                        else "unknown"
                    ),
                    "episode_still_refreshed_at": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("still_refreshed_at")
                        if row.season is not None and row.episode is not None
                        else None
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
                    "episode_trakt_status": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("trakt_details_status", "unknown")
                        if row.season is not None and row.episode is not None
                        else "unknown"
                    ),
                    "episode_trakt_refreshed_at": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("trakt_details_refreshed_at")
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
                    "episode_imdb_status": (
                        "ready"
                        if (
                            (
                                (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_rating")
                                or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_rating")
                            ) is not None
                            and (
                                (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_votes")
                                or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_votes")
                            ) is not None
                        )
                        else (
                            "checked_no_data"
                            if row.season is not None
                            and row.episode is not None
                            and (
                                (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_id")
                                or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_id")
                            )
                            else "unknown"
                        )
                        if row.season is not None and row.episode is not None
                        else "unknown"
                    ),
                    "event_rating": row.rating,
                    "title_rating": ratings.get(row.title_trakt_id),
                    "display_rating": display_ratings_by_row_id.get(int(row.id)),
                }
                for row in rows
            ]

    def history_title_summaries(
        self,
        *,
        title_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        title_filter: str | None = None,
        rated_only: bool = False,
    ) -> list[dict]:
        with self._db.session() as session:
            all_rows = self._history.list_filtered(
                session,
                title_type=title_type,
                title_filter=title_filter,
                action="watched",
            )
            all_rows = self._dedupe_history_rows(all_rows)
            rated_map = self._history.latest_rated_map(
                session,
                title_type=title_type,
                title_filter=title_filter,
            )
            cached_title_ratings, cached_episode_ratings = self._episode_metadata.load_cached_trakt_rating_maps()
            title_episode_stats = self._build_title_episode_rating_stats(
                all_rows,
                rated_map=rated_map,
                cached_episode_ratings=cached_episode_ratings,
            )
            title_groups: dict[tuple[str, int], dict] = {}
            for row in all_rows:
                key = (row.title_type, row.title_trakt_id)
                display_rating = self._display_rating_for_row(
                    row=row,
                    rated_map=rated_map,
                    cached_title_ratings=cached_title_ratings,
                    cached_episode_ratings=cached_episode_ratings,
                )
                group = title_groups.get(key)
                if group is None:
                    stats = title_episode_stats.get(row.title_trakt_id) or {}
                    group = {
                        "title_trakt_id": row.title_trakt_id,
                        "title": row.title,
                        "type": row.title_type,
                        "last_watched_at": row.watched_at,
                        "watched_count": 0,
                        "title_episode_avg_rating": stats.get("avg_rating"),
                        "title_episode_rated_count": stats.get("rated_count", 0),
                        "movie_display_rating": display_rating if row.title_type == "movie" else None,
                        "latest_season": row.season,
                        "latest_episode": row.episode,
                    }
                    title_groups[key] = group
                group["watched_count"] += 1
                if row.title_type == "movie" and group.get("movie_display_rating") is None:
                    group["movie_display_rating"] = display_rating
            trakt_ids = [int(group["title_trakt_id"]) for group in title_groups.values()]
            user_title_ratings = self._user_states.ratings_by_trakt_ids(session, trakt_ids)
            filtered_groups = []
            for group in title_groups.values():
                trakt_id = int(group["title_trakt_id"])
                if group["type"] == "show":
                    my_rating = group.get("title_episode_avg_rating")
                else:
                    my_rating = group.get("movie_display_rating") or user_title_ratings.get(trakt_id)
                group["my_rating"] = my_rating
                group["title_rating"] = user_title_ratings.get(trakt_id)
                if rated_only and my_rating is None:
                    continue
                filtered_groups.append(group)
            if offset:
                filtered_groups = filtered_groups[offset:]
            if limit is not None:
                filtered_groups = filtered_groups[:limit]
            title_models = {
                int(group["title_trakt_id"]): self._titles.get_title(session, int(group["title_trakt_id"]))
                for group in filtered_groups
            }
            return [
                {
                    "title_key": f"{group['type']}:{group['title_trakt_id']}",
                    "title_trakt_id": group["title_trakt_id"],
                    "title": group["title"],
                    "title_slug": (title_models.get(int(group["title_trakt_id"])).slug if title_models.get(int(group["title_trakt_id"])) is not None else ""),
                    "poster_url": (title_models.get(int(group["title_trakt_id"])).poster_url if title_models.get(int(group["title_trakt_id"])) is not None else ""),
                    "title_poster_status": (title_models.get(int(group["title_trakt_id"])).poster_status if title_models.get(int(group["title_trakt_id"])) is not None else "unknown"),
                    "title_poster_refreshed_at": (
                        title_models.get(int(group["title_trakt_id"])).poster_refreshed_at if title_models.get(int(group["title_trakt_id"])) is not None else None
                    ),
                    "backdrop_url": (title_models.get(int(group["title_trakt_id"])).backdrop_url if title_models.get(int(group["title_trakt_id"])) is not None else ""),
                    "title_backdrop_status": (
                        title_models.get(int(group["title_trakt_id"])).backdrop_status if title_models.get(int(group["title_trakt_id"])) is not None else "unknown"
                    ),
                    "title_backdrop_refreshed_at": (
                        title_models.get(int(group["title_trakt_id"])).backdrop_refreshed_at if title_models.get(int(group["title_trakt_id"])) is not None else None
                    ),
                    "title_trakt_rating": (title_models.get(int(group["title_trakt_id"])).trakt_rating if title_models.get(int(group["title_trakt_id"])) is not None else None),
                    "title_trakt_votes": (title_models.get(int(group["title_trakt_id"])).trakt_votes if title_models.get(int(group["title_trakt_id"])) is not None else None),
                    "title_imdb_rating": (title_models.get(int(group["title_trakt_id"])).imdb_rating if title_models.get(int(group["title_trakt_id"])) is not None else None),
                    "title_imdb_votes": (title_models.get(int(group["title_trakt_id"])).imdb_votes if title_models.get(int(group["title_trakt_id"])) is not None else None),
                    "title_ratings_status": (title_models.get(int(group["title_trakt_id"])).ratings_status if title_models.get(int(group["title_trakt_id"])) is not None else "unknown"),
                    "title_ratings_refreshed_at": (
                        title_models.get(int(group["title_trakt_id"])).ratings_refreshed_at if title_models.get(int(group["title_trakt_id"])) is not None else None
                    ),
                    "title_episode_avg_rating": group.get("title_episode_avg_rating"),
                    "title_episode_rated_count": group.get("title_episode_rated_count", 0),
                    "my_rating": group.get("my_rating"),
                    "title_rating": group.get("title_rating"),
                    "type": group["type"],
                    "last_watched_at": group.get("last_watched_at"),
                    "watched_count": group.get("watched_count", 0),
                    "latest_season": group.get("latest_season"),
                    "latest_episode": group.get("latest_episode"),
                }
                for group in filtered_groups
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

    @staticmethod
    def _build_title_episode_rating_stats(
        rows: list,
        *,
        rated_map: dict[tuple[int, int | None, int | None], int],
        cached_episode_ratings: dict[tuple[int, int, int], int],
    ) -> dict[int, dict]:
        totals: dict[int, dict[str, float | int]] = {}
        for row in rows:
            if row.title_type != "show" or row.season is None or row.episode is None:
                continue
            display_rating = (
                row.rating
                or rated_map.get((row.title_trakt_id, row.season, row.episode))
                or cached_episode_ratings.get((row.title_trakt_id, row.season, row.episode))
            )
            if display_rating is None:
                continue
            item = totals.setdefault(row.title_trakt_id, {"sum_rating": 0.0, "rated_count": 0})
            item["sum_rating"] += float(display_rating)
            item["rated_count"] += 1
        return {
            trakt_id: {
                "avg_rating": (item["sum_rating"] / item["rated_count"]) if item["rated_count"] else None,
                "rated_count": int(item["rated_count"]),
            }
            for trakt_id, item in totals.items()
        }

    @staticmethod
    def _display_rating_for_row(
        *,
        row,
        rated_map: dict[tuple[int, int | None, int | None], int],
        cached_title_ratings: dict[int, int],
        cached_episode_ratings: dict[tuple[int, int, int], int],
    ) -> int | None:
        if row.title_type == "show":
            return (
                row.rating
                or rated_map.get((row.title_trakt_id, row.season, row.episode))
                or cached_episode_ratings.get((row.title_trakt_id, row.season, row.episode))
            )
        return (
            row.rating
            or rated_map.get((row.title_trakt_id, None, None))
            or cached_title_ratings.get(row.title_trakt_id)
        )
