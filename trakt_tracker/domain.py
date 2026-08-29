from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


TitleType = Literal["movie", "show"]


class ProgressView(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DROPPED = "dropped"


class ProgressSortMode(str, Enum):
    LAST_WATCHED = "last_watched"
    EPISODE_RELEASE = "episode_release"
    RELEASE_YEAR = "release_year"


def synthetic_episode_id(show_id: int, season: int, episode: int) -> int:
    """Stable local identity for an episode that has no tracker episode id."""
    return -(
        max(1, int(show_id)) * 1_000_000
        + max(0, int(season)) * 10_000
        + max(1, int(episode))
    )


@dataclass(slots=True)
class TitleSummary:
    trakt_id: int
    title_type: TitleType
    title: str
    year: int | None = None
    overview: str = ""
    poster_url: str = ""
    backdrop_url: str = ""
    status: str = ""
    slug: str = ""
    trakt_rating: float | None = None
    trakt_votes: int | None = None
    tmdb_id: int | None = None
    tmdb_rating: float | None = None
    tmdb_votes: int | None = None
    imdb_id: str = ""
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    ratings_status: str = "unknown"
    ratings_refreshed_at: datetime | None = None
    poster_refreshed_at: datetime | None = None
    backdrop_refreshed_at: datetime | None = None
    title_episode_avg_rating: float | None = None
    is_watchlisted: bool = False
    watchlisted_at: datetime | None = None
    released_at: datetime | None = None
    explore_metric_kind: str = ""
    explore_metric_count: int | None = None
    catalog_actions_available: bool = True
    is_release_tracked: bool = False
    release_acknowledged: bool = False
    release_distance_text: str = ""
    is_future_release: bool = False
    is_in_history: bool = False


@dataclass(slots=True)
class ExploreResultPage:
    items: list[TitleSummary]
    page: int
    page_count: int


@dataclass(slots=True)
class EpisodeSummary:
    trakt_id: int
    season: int
    number: int
    title: str
    still_url: str = ""
    still_status: str = "unknown"
    still_refreshed_at: datetime | None = None
    trakt_rating: float | None = None
    trakt_votes: int | None = None
    trakt_details_status: str = "unknown"
    trakt_details_refreshed_at: datetime | None = None
    imdb_id: str = ""
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    imdb_season: int | None = None
    imdb_episode: int | None = None
    tmdb_season: int | None = None
    tmdb_episode: int | None = None
    imdb_status: str = "unknown"
    first_aired: datetime | None = None
    runtime: int | None = None
    overview: str = ""
    tmdb_rating: float | None = None
    tmdb_votes: int | None = None


@dataclass(slots=True)
class HistoryItemInput:
    title_type: TitleType
    trakt_id: int
    watched_at: datetime | None
    season: int | None = None
    episode: int | None = None
    title: str = ""


@dataclass(slots=True)
class RatingInput:
    title_type: TitleType
    trakt_id: int
    rating: int
    season: int | None = None
    episode: int | None = None


@dataclass(slots=True)
class ProgressSnapshot:
    trakt_id: int
    title: str
    completed: int
    aired: int
    percent_completed: float
    slug: str = ""
    next_episode: EpisodeSummary | None = None
    last_episode: EpisodeSummary | None = None
    poster_url: str = ""
    poster_status: str = "unknown"
    poster_refreshed_at: datetime | None = None
    status: str = ""
    title_trakt_rating: float | None = None
    title_trakt_votes: int | None = None
    title_imdb_rating: float | None = None
    title_imdb_votes: int | None = None
    title_ratings_status: str = "unknown"
    title_ratings_refreshed_at: datetime | None = None
    title_episode_avg_rating: float | None = None
    is_dropped: bool = False
    is_paused: bool = False
    last_watched_at: datetime | None = None
    title_year: int | None = None
    provider: str = "trakt"
    tmdb_id: int | None = None
    title_tmdb_rating: float | None = None
    title_tmdb_votes: int | None = None


@dataclass(slots=True)
class CalendarEntry:
    show_trakt_id: int
    show_title: str
    episode: EpisodeSummary


@dataclass(slots=True)
class DashboardState:
    in_progress: list[ProgressSnapshot] = field(default_factory=list)
    recent_history: list[dict] = field(default_factory=list)
    upcoming: list[CalendarEntry] = field(default_factory=list)
