from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from trakt_tracker.application.metadata_refresh_policy import ASSET_KIND_STILL, TRIGGER_PAGE_CONTEXT
from trakt_tracker.domain import EpisodeSummary, HistoryItemInput


SEASON_LAYOUT_TRAKT = "trakt"
SEASON_LAYOUT_IMDB = "imdb"
SEASON_LAYOUTS = {SEASON_LAYOUT_TRAKT, SEASON_LAYOUT_IMDB}


def normalize_season_layout(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SEASON_LAYOUTS:
        raise ValueError("Unsupported season layout.")
    return normalized


@dataclass(slots=True)
class SearchWatchEpisode:
    season: int
    number: int
    title: str
    still_url: str = ""
    still_status: str = "unknown"
    trakt_rating: float | None = None
    trakt_votes: int | None = None
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    imdb_season: int | None = None
    imdb_episode: int | None = None
    first_aired: datetime | None = None
    is_released: bool = True
    is_watched: bool = False
    user_rating: int | None = None


@dataclass(slots=True)
class SearchWatchSeason:
    season: int
    label: str
    episodes: list[SearchWatchEpisode] = field(default_factory=list)
    is_default: bool = False
    bulk_allowed: bool = True
    bulk_released_count: int | None = None
    bulk_released_watched_count: int | None = None
    bulk_watched_count: int | None = None

    @property
    def watched_count(self) -> int:
        return sum(1 for episode in self.episodes if episode.is_watched)

    @property
    def released_count(self) -> int:
        return sum(1 for episode in self.episodes if episode.is_released)

    @property
    def released_watched_count(self) -> int:
        return sum(1 for episode in self.episodes if episode.is_released and episode.is_watched)

    @property
    def action_released_count(self) -> int:
        return self.released_count if self.bulk_released_count is None else self.bulk_released_count

    @property
    def action_released_watched_count(self) -> int:
        if self.bulk_released_watched_count is None:
            return self.released_watched_count
        return self.bulk_released_watched_count

    @property
    def action_watched_count(self) -> int:
        return self.watched_count if self.bulk_watched_count is None else self.bulk_watched_count


@dataclass(slots=True)
class SearchShowWatchPanel:
    trakt_id: int
    title: str
    poster_url: str = ""
    slug: str = ""
    title_trakt_rating: float | None = None
    title_trakt_votes: int | None = None
    title_imdb_rating: float | None = None
    title_imdb_votes: int | None = None
    title_ratings_status: str = "unknown"
    seasons: list[SearchWatchSeason] = field(default_factory=list)
    default_episode_key: tuple[int, int] | None = None
    watched_frontier_key: tuple[int, int] | None = None
    season_layout: str = SEASON_LAYOUT_TRAKT
    imdb_mapping_complete: bool = True
    imdb_mapping_pending: bool = False

    @property
    def watched_count(self) -> int:
        return sum(season.watched_count for season in self.seasons)

    @property
    def released_count(self) -> int:
        return sum(
            1
            for season in self.seasons
            for episode in season.episodes
            if episode.season >= 1 and episode.is_released
        )

    @property
    def released_watched_count(self) -> int:
        return sum(
            1
            for season in self.seasons
            for episode in season.episodes
            if episode.season >= 1 and episode.is_released and episode.is_watched
        )


class SearchWatchService:
    def __init__(self, db, auth_service, titles_repo, history_repo, episode_repo, history_service, episode_metadata=None) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles_repo
        self._history = history_repo
        self._episode_repo = episode_repo
        self._history_service = history_service
        self._episode_metadata = episode_metadata

    def load_show_panel(
        self,
        trakt_id: int,
        default_season: int | None = None,
        *,
        season_layout: str = SEASON_LAYOUT_TRAKT,
    ) -> SearchShowWatchPanel:
        normalized_layout = normalize_season_layout(season_layout)
        with self._db.session() as session:
            title_row = self._titles.get_title(session, trakt_id)
            episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
            watched_keys = self._history.watched_episode_keys(session, trakt_id)
            rated_map = self._history.latest_show_episode_ratings(session, trakt_id)
        default_episode_key = self._default_episode_key(episode_rows, watched_keys)
        watched_frontier_key = max((key for key in watched_keys if key[0] > 0), default=None)
        imdb_mapping_complete = self._imdb_mapping_complete(episode_rows)
        imdb_mapping_pending = (
            normalized_layout == SEASON_LAYOUT_IMDB
            and not imdb_mapping_complete
            and self._imdb_mapping_pending(trakt_id)
        )
        grouped_rows: dict[int, list[dict]] = {}
        for row in episode_rows:
            if row.get("season") is None or row.get("number") is None:
                continue
            display_season = int(row["season"])
            if normalized_layout == SEASON_LAYOUT_IMDB and row.get("imdb_season") is not None:
                display_season = int(row["imdb_season"])
            grouped_rows.setdefault(display_season, []).append(row)
        default_season_number = self._display_season_for_key(
            episode_rows,
            default_episode_key,
            season_layout=normalized_layout,
        )
        if default_season_number is None:
            default_season_number = self._default_season_number(episode_rows)
        if default_season is not None:
            if default_season in grouped_rows:
                default_season_number = default_season
        seasons: list[SearchWatchSeason] = []
        for season_number in sorted(grouped_rows):
            rows = sorted(
                grouped_rows[season_number],
                key=lambda row: self._display_episode_sort_key(row, season_layout=normalized_layout),
            )
            episodes = [
                self._episode_from_row(row, watched_keys, rated_map)
                for row in rows
            ]
            bulk_episodes = [
                episode
                for row, episode in zip(rows, episodes, strict=True)
                if normalized_layout == SEASON_LAYOUT_TRAKT
                or (
                    row.get("imdb_season") == season_number
                    and row.get("imdb_episode") is not None
                )
            ]
            seasons.append(
                SearchWatchSeason(
                    season=season_number,
                    label=f"S{season_number}",
                    episodes=episodes,
                    is_default=season_number == default_season_number,
                    bulk_allowed=(
                        normalized_layout == SEASON_LAYOUT_TRAKT
                        or (imdb_mapping_complete and season_number > 0)
                    ),
                    bulk_released_count=sum(1 for episode in bulk_episodes if episode.is_released),
                    bulk_released_watched_count=sum(
                        1 for episode in bulk_episodes if episode.is_released and episode.is_watched
                    ),
                    bulk_watched_count=sum(1 for episode in bulk_episodes if episode.is_watched),
                )
            )
        return SearchShowWatchPanel(
            trakt_id=trakt_id,
            title=(title_row.title if title_row is not None and title_row.title else f"Show {trakt_id}"),
            poster_url=(title_row.poster_url if title_row is not None else ""),
            slug=(title_row.slug if title_row is not None else ""),
            title_trakt_rating=(title_row.trakt_rating if title_row is not None else None),
            title_trakt_votes=(title_row.trakt_votes if title_row is not None else None),
            title_imdb_rating=(title_row.imdb_rating if title_row is not None else None),
            title_imdb_votes=(title_row.imdb_votes if title_row is not None else None),
            title_ratings_status=(title_row.ratings_status if title_row is not None else "unknown"),
            seasons=seasons,
            default_episode_key=default_episode_key,
            watched_frontier_key=watched_frontier_key,
            season_layout=normalized_layout,
            imdb_mapping_complete=imdb_mapping_complete,
            imdb_mapping_pending=imdb_mapping_pending,
        )

    def hydrate_show_episodes(self, trakt_id: int) -> bool:
        """Fetch a missing episode list outside of a panel HTTP request."""
        client = self._auth.get_client()
        episodes = client.get_show_episodes(trakt_id)
        with self._db.session() as session:
            self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
        if self._episode_metadata is not None:
            self._episode_metadata.repair_episode_imdb_ratings(trakt_id)
        return bool(episodes)

    def enrich_missing_stills(self, trakt_id: int, season: int) -> bool:
        if self._episode_metadata is None:
            return False
        with self._db.session() as session:
            episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
        keys = [
            (trakt_id, int(row["season"]), int(row["number"]))
            for row in episode_rows
            if row.get("season") is not None
            and row.get("number") is not None
            and int(row["season"]) == int(season)
            and self._is_released(row.get("first_aired"))
            and not row.get("still_url")
        ]
        if not keys:
            return False
        return self._episode_metadata.enrich_episode_stills(
            keys,
            trigger=TRIGGER_PAGE_CONTEXT,
            requested_parts=(ASSET_KIND_STILL,),
        )

    def repair_imdb_seasons(self, trakt_id: int) -> int:
        if self._episode_metadata is None:
            return 0
        return int(self._episode_metadata.repair_episode_imdb_ratings(int(trakt_id)) or 0)

    def mark_watch(
        self,
        *,
        title_type: str,
        trakt_id: int,
        title: str,
        scope: str,
        watched_at: datetime | None,
        season: int | None = None,
        episode: int | None = None,
        season_layout: str = SEASON_LAYOUT_TRAKT,
    ) -> int:
        normalized_type = "show" if title_type == "show" else "movie"
        if normalized_type == "movie":
            self._history_service.add_history_item(
                HistoryItemInput(
                    title_type="movie",
                    trakt_id=trakt_id,
                    watched_at=watched_at,
                    title=title,
                )
            )
            return 1
        selected = self._select_show_episodes(
            trakt_id,
            scope=scope,
            season=season,
            episode=episode,
            season_layout=season_layout,
            exclude_watched=scope == "season",
        )
        if not selected:
            raise RuntimeError("No released episodes matched this action.")
        self._history_service.add_history_items(
            [
                HistoryItemInput(
                    title_type="show",
                    trakt_id=trakt_id,
                    watched_at=watched_at,
                    season=item.season,
                    episode=item.number,
                    title=title,
                )
                for item in selected
            ]
        )
        return len(selected)

    def unmark_episode(self, *, trakt_id: int, season: int, episode: int) -> dict:
        return self._history_service.remove_episode_watch(
            show_trakt_id=trakt_id,
            season=season,
            episode=episode,
        )

    def unmark_scope(
        self,
        *,
        title_type: str,
        trakt_id: int,
        scope: str,
        season: int | None = None,
        season_layout: str = SEASON_LAYOUT_TRAKT,
    ) -> dict:
        normalized_layout = normalize_season_layout(season_layout)
        if title_type == "show" and scope == "season" and normalized_layout == SEASON_LAYOUT_IMDB:
            selected = self._select_show_episodes(
                trakt_id,
                scope=scope,
                season=season,
                episode=None,
                season_layout=normalized_layout,
                released_only=False,
            )
            return self._history_service.remove_watch_scope(
                title_type=title_type,
                trakt_id=trakt_id,
                scope=scope,
                season=season,
                episode_keys={(item.season, item.number) for item in selected},
                season_layout=normalized_layout,
            )
        return self._history_service.remove_watch_scope(
            title_type=title_type,
            trakt_id=trakt_id,
            scope=scope,
            season=season,
            season_layout=normalized_layout,
        )

    def restore_episode(self, **restore) -> None:
        self._history_service.restore_episode_watch(
            show_trakt_id=int(restore["trakt_id"]),
            title=str(restore.get("title") or ""),
            season=int(restore["season"]),
            episode=int(restore["episode"]),
            watched_at=restore["watched_at"],
            watched_at_known=bool(restore.get("watched_at_known", True)),
        )

    def restore_scope(self, *, items: list[dict]) -> None:
        self._history_service.restore_watch_scope(items=items)

    def _select_show_episodes(
        self,
        trakt_id: int,
        *,
        scope: str,
        season: int | None,
        episode: int | None,
        season_layout: str,
        exclude_watched: bool = False,
        released_only: bool = True,
    ) -> list[EpisodeSummary]:
        normalized_layout = normalize_season_layout(season_layout)
        rows = self._load_episode_summaries(trakt_id)
        if scope == "season" and normalized_layout == SEASON_LAYOUT_IMDB:
            self._assert_imdb_bulk_ready(rows)
        watched_keys: set[tuple[int, int]] = set()
        if exclude_watched:
            with self._db.session() as session:
                watched_keys = self._history.watched_episode_keys(session, trakt_id)
        result: list[EpisodeSummary] = []
        for item in rows:
            if released_only and not self._is_released(item.first_aired):
                continue
            if scope == "title":
                if item.season >= 1:
                    result.append(item)
                continue
            if scope == "season":
                matches_season = (
                    item.imdb_season == season and item.imdb_episode is not None
                    if normalized_layout == SEASON_LAYOUT_IMDB
                    else item.season == season
                )
                if matches_season and (item.season, item.number) not in watched_keys:
                    result.append(item)
                continue
            if scope == "episode" and season is not None and episode is not None:
                if item.season == season and item.number == episode:
                    result.append(item)
        return result

    def _load_episode_summaries(self, trakt_id: int) -> list[EpisodeSummary]:
        with self._db.session() as session:
            rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
        if not rows:
            client = self._auth.get_client()
            episodes = client.get_show_episodes(trakt_id)
            with self._db.session() as session:
                self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
                rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
        return [
            EpisodeSummary(
                trakt_id=int(row.get("episode_trakt_id") or 0),
                season=int(row["season"]),
                number=int(row["number"]),
                title=str(row.get("title") or ""),
                imdb_id=str(row.get("imdb_id") or ""),
                imdb_rating=row.get("imdb_rating"),
                imdb_votes=row.get("imdb_votes"),
                imdb_season=row.get("imdb_season"),
                imdb_episode=row.get("imdb_episode"),
                imdb_status=str(row.get("imdb_match_status") or "unknown"),
                first_aired=row.get("first_aired"),
            )
            for row in rows
            if row.get("season") is not None and row.get("number") is not None
        ]

    def _imdb_mapping_pending(self, trakt_id: int) -> bool:
        if self._episode_metadata is None:
            return False
        checker = getattr(self._episode_metadata, "needs_episode_imdb_reconciliation", None)
        return bool(checker and checker(int(trakt_id)))

    @classmethod
    def _imdb_mapping_complete(cls, rows: list[dict]) -> bool:
        return all(
            row.get("imdb_season") is not None and row.get("imdb_episode") is not None
            for row in rows
            if row.get("season") is not None
            and int(row["season"]) >= 1
            and cls._is_released(row.get("first_aired"))
        )

    @classmethod
    def _assert_imdb_bulk_ready(cls, rows: list[EpisodeSummary]) -> None:
        if all(
            item.imdb_season is not None and item.imdb_episode is not None
            for item in rows
            if item.season >= 1 and cls._is_released(item.first_aired)
        ):
            return
        raise RuntimeError("IMDb season mapping is incomplete. Try again after metadata refresh.")

    @staticmethod
    def _display_episode_sort_key(row: dict, *, season_layout: str) -> tuple[int, int, int]:
        if (
            season_layout == SEASON_LAYOUT_IMDB
            and row.get("imdb_season") is not None
            and row.get("imdb_episode") is not None
        ):
            return (int(row["imdb_episode"]), int(row.get("season") or 0), int(row.get("number") or 0))
        return (int(row.get("number") or 0), int(row.get("season") or 0), int(row.get("number") or 0))

    @staticmethod
    def _display_season_for_key(
        rows: list[dict],
        key: tuple[int, int] | None,
        *,
        season_layout: str,
    ) -> int | None:
        if key is None:
            return None
        for row in rows:
            if row.get("season") is None or row.get("number") is None:
                continue
            if (int(row["season"]), int(row["number"])) != key:
                continue
            if season_layout == SEASON_LAYOUT_IMDB and row.get("imdb_season") is not None:
                return int(row["imdb_season"])
            return int(row["season"])
        return None

    @classmethod
    def _episode_from_row(
        cls,
        row: dict,
        watched_keys: set[tuple[int, int]],
        rated_map: dict[tuple[int, int], int],
    ) -> SearchWatchEpisode:
        season = int(row["season"])
        number = int(row["number"])
        return SearchWatchEpisode(
            season=season,
            number=number,
            title=str(row.get("title") or f"Episode {number}"),
            still_url=str(row.get("still_url") or ""),
            still_status=str(row.get("still_status") or "unknown"),
            trakt_rating=row.get("trakt_rating"),
            trakt_votes=row.get("trakt_votes"),
            imdb_rating=row.get("imdb_rating"),
            imdb_votes=row.get("imdb_votes"),
            imdb_season=row.get("imdb_season"),
            imdb_episode=row.get("imdb_episode"),
            first_aired=row.get("first_aired"),
            is_released=cls._is_released(row.get("first_aired")),
            is_watched=(season, number) in watched_keys,
            user_rating=rated_map.get((season, number)),
        )

    @staticmethod
    def _default_season_number(rows: list[dict]) -> int:
        season_numbers = sorted({int(row["season"]) for row in rows if row.get("season") is not None})
        if 1 in season_numbers:
            return 1
        regular = [season for season in season_numbers if season > 0]
        if regular:
            return regular[0]
        return season_numbers[0] if season_numbers else 1

    @classmethod
    def _default_episode_key(cls, rows: list[dict], watched_keys: set[tuple[int, int]]) -> tuple[int, int] | None:
        for row in rows:
            if row.get("season") is None or row.get("number") is None:
                continue
            key = (int(row["season"]), int(row["number"]))
            if key[0] < 1 or key in watched_keys or not cls._is_released(row.get("first_aired")):
                continue
            return key
        regular_seasons = sorted(
            {
                int(row["season"])
                for row in rows
                if row.get("season") is not None and int(row["season"]) > 0
            }
        )
        if not regular_seasons:
            return None
        last_season = regular_seasons[-1]
        episode_numbers = [
            int(row["number"])
            for row in rows
            if row.get("number") is not None and int(row.get("season") or 0) == last_season
        ]
        return (last_season, min(episode_numbers)) if episode_numbers else None

    @staticmethod
    def _is_released(first_aired: datetime | None) -> bool:
        if first_aired is None:
            return True
        normalized = first_aired if first_aired.tzinfo is not None else first_aired.replace(tzinfo=UTC)
        return normalized.astimezone(UTC) <= datetime.now(tz=UTC)
