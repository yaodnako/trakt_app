from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from trakt_tracker.application.metadata_refresh_policy import ASSET_KIND_STILL, TRIGGER_PAGE_CONTEXT
from trakt_tracker.domain import EpisodeSummary, HistoryItemInput


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

    @property
    def watched_count(self) -> int:
        return sum(1 for episode in self.episodes if episode.is_watched)

    @property
    def released_count(self) -> int:
        return sum(1 for episode in self.episodes if episode.is_released)


@dataclass(slots=True)
class SearchShowWatchPanel:
    trakt_id: int
    title: str
    poster_url: str = ""
    slug: str = ""
    seasons: list[SearchWatchSeason] = field(default_factory=list)
    default_episode_key: tuple[int, int] | None = None

    @property
    def watched_count(self) -> int:
        return sum(season.watched_count for season in self.seasons)

    @property
    def released_count(self) -> int:
        return sum(season.released_count for season in self.seasons if season.season >= 1)

    @property
    def released_watched_count(self) -> int:
        return sum(
            1
            for season in self.seasons
            if season.season >= 1
            for episode in season.episodes
            if episode.is_released and episode.is_watched
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

    def load_show_panel(self, trakt_id: int, default_season: int | None = None) -> SearchShowWatchPanel:
        with self._db.session() as session:
            title_row = self._titles.get_title(session, trakt_id)
            episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
            watched_keys = self._history.watched_episode_keys(session, trakt_id)
            rated_map = self._history.latest_show_episode_ratings(session, trakt_id)
        default_episode_key = self._default_episode_key(episode_rows, watched_keys)
        default_season_number = default_episode_key[0] if default_episode_key is not None else self._default_season_number(episode_rows)
        if default_season is not None:
            season_numbers = {int(row["season"]) for row in episode_rows if row.get("season") is not None}
            if default_season in season_numbers:
                default_season_number = default_season
        seasons: list[SearchWatchSeason] = []
        for season_number in sorted({int(row["season"]) for row in episode_rows if row.get("season") is not None}):
            episodes = [
                self._episode_from_row(row, watched_keys, rated_map)
                for row in episode_rows
                if int(row.get("season") or 0) == season_number
            ]
            seasons.append(
                SearchWatchSeason(
                    season=season_number,
                    label=f"S{season_number}",
                    episodes=episodes,
                    is_default=season_number == default_season_number,
                )
            )
        return SearchShowWatchPanel(
            trakt_id=trakt_id,
            title=(title_row.title if title_row is not None and title_row.title else f"Show {trakt_id}"),
            poster_url=(title_row.poster_url if title_row is not None else ""),
            slug=(title_row.slug if title_row is not None else ""),
            seasons=seasons,
            default_episode_key=default_episode_key,
        )

    def hydrate_show_episodes(self, trakt_id: int) -> bool:
        """Fetch a missing episode list outside of a panel HTTP request."""
        client = self._auth.get_client()
        episodes = client.get_show_episodes(trakt_id)
        with self._db.session() as session:
            self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
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
        selected = self._select_show_episodes(trakt_id, scope=scope, season=season, episode=episode)
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
    ) -> dict:
        return self._history_service.remove_watch_scope(
            title_type=title_type,
            trakt_id=trakt_id,
            scope=scope,
            season=season,
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
    ) -> list[EpisodeSummary]:
        rows = self._load_episode_summaries(trakt_id)
        result: list[EpisodeSummary] = []
        for item in rows:
            if not self._is_released(item.first_aired):
                continue
            if scope == "title":
                if item.season >= 1:
                    result.append(item)
                continue
            if scope == "season":
                if season is not None and item.season == season:
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
                first_aired=row.get("first_aired"),
            )
            for row in rows
            if row.get("season") is not None and row.get("number") is not None
        ]

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
