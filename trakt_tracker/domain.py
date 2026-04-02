from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


TitleType = Literal["movie", "show"]


@dataclass(slots=True)
class TitleSummary:
    trakt_id: int
    title_type: TitleType
    title: str
    year: int | None = None
    overview: str = ""
    poster_url: str = ""
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


@dataclass(slots=True)
class EpisodeSummary:
    trakt_id: int
    season: int
    number: int
    title: str
    still_url: str = ""
    trakt_rating: float | None = None
    trakt_votes: int | None = None
    imdb_id: str = ""
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    first_aired: datetime | None = None
    runtime: int | None = None
    overview: str = ""


@dataclass(slots=True)
class HistoryItemInput:
    title_type: TitleType
    trakt_id: int
    watched_at: datetime
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
    next_episode: EpisodeSummary | None = None
    last_episode: EpisodeSummary | None = None
    poster_url: str = ""
    status: str = ""
    is_dropped: bool = False


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
