from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from trakt_tracker.application.metadata_refresh_policy import ASSET_KIND_STILL, TRIGGER_MANUAL_REPAIR
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
    seasons: list[SearchWatchSeason] = field(default_factory=list)


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
        if not episode_rows:
            client = self._auth.get_client()
            episodes = client.get_show_episodes(trakt_id)
            with self._db.session() as session:
                self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
                title_row = self._titles.get_title(session, trakt_id)
                episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
                watched_keys = self._history.watched_episode_keys(session, trakt_id)
        default_season_number = self._default_season_number(episode_rows)
        if default_season is not None:
            season_numbers = {int(row["season"]) for row in episode_rows if row.get("season") is not None}
            if default_season in season_numbers:
                default_season_number = default_season
        if self._episode_metadata is not None:
            default_season_keys = [
                (trakt_id, int(row["season"]), int(row["number"]))
                for row in episode_rows
                if row.get("season") is not None
                and row.get("number") is not None
                and int(row["season"]) == default_season_number
                and self._is_released(row.get("first_aired"))
                and not row.get("still_url")
            ]
            if default_season_keys:
                self._episode_metadata.enrich_episode_stills(
                    default_season_keys,
                    trigger=TRIGGER_MANUAL_REPAIR,
                    requested_parts=(ASSET_KIND_STILL,),
                )
                with self._db.session() as session:
                    title_row = self._titles.get_title(session, trakt_id)
                    episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
                    watched_keys = self._history.watched_episode_keys(session, trakt_id)
        seasons: list[SearchWatchSeason] = []
        for season_number in sorted({int(row["season"]) for row in episode_rows if row.get("season") is not None}):
            episodes = [
                self._episode_from_row(row, watched_keys)
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
            seasons=seasons,
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
    def _episode_from_row(cls, row: dict, watched_keys: set[tuple[int, int]]) -> SearchWatchEpisode:
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

    @staticmethod
    def _is_released(first_aired: datetime | None) -> bool:
        if first_aired is None:
            return True
        normalized = first_aired if first_aired.tzinfo is not None else first_aired.replace(tzinfo=UTC)
        return normalized.astimezone(UTC) <= datetime.now(tz=UTC)
