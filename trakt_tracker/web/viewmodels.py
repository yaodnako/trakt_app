from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_TITLE_RATINGS,
    EPISODE_RATINGS_READY_REFRESH_SECONDS,
    RATINGS_EMPTY_REFRESH_SECONDS,
    TITLE_RATINGS_READY_REFRESH_SECONDS,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
    metadata_refresh_due,
)
from trakt_tracker.domain import TitleSummary


SEARCH_SORT_MODES = ("IMDb votes", "Trakt votes", "Alphabetical")
DEFAULT_SEARCH_SORT_MODE = "IMDb votes"
WATCHLIST_SORT_MODES = ("Recently added", "Release date", "IMDb rating", "Alphabetical")
DEFAULT_WATCHLIST_SORT_MODE = "Recently added"
PROGRESS_SORT_OPTIONS = (
    ("episode_release", "Episode release"),
    ("last_watched", "Last watched"),
    ("release_year", "Release year"),
)
DEFAULT_PROGRESS_SORT_MODE = "episode_release"
DEFAULT_PROGRESS_SORT_DIRECTION = "desc"
HISTORY_PAGE_SIZE = 50


def normalize_history_view(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"title", "titles"}:
        return "titles"
    return "episodes"


def normalize_title_type(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if normalized in {"movie", "show"}:
        return normalized
    return None


def normalize_search_sort_mode(value: str | None, fallback: str | None = None) -> str:
    normalized = (value or "").strip()
    if normalized in SEARCH_SORT_MODES:
        return normalized
    fallback_normalized = (fallback or "").strip()
    if fallback_normalized in SEARCH_SORT_MODES:
        return fallback_normalized
    return DEFAULT_SEARCH_SORT_MODE


def saved_search_matches(saved_state: dict | None, query: str, title_type: str | None) -> bool:
    if not saved_state:
        return False
    saved_query = str(saved_state.get("query", "") or "").strip()
    saved_type = normalize_title_type(saved_state.get("title_type"))
    expected_query = query.strip()
    return bool(expected_query) and saved_query == expected_query and saved_type == title_type and bool(saved_state.get("results"))


def sort_search_results(results: list[TitleSummary], mode: str) -> list[TitleSummary]:
    normalized_mode = normalize_search_sort_mode(mode)
    if normalized_mode == "IMDb votes":
        return sorted(
            results,
            key=lambda item: (item.imdb_votes or 0, item.imdb_rating or 0.0, (item.title or "").lower()),
            reverse=True,
        )
    if normalized_mode == "Alphabetical":
        return sorted(results, key=lambda item: ((item.title or "").lower(), item.year or 0))
    return sorted(
        results,
        key=lambda item: (item.trakt_votes or 0, item.trakt_rating or 0.0, (item.title or "").lower()),
        reverse=True,
    )


def normalize_watchlist_sort_mode(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized if normalized in WATCHLIST_SORT_MODES else DEFAULT_WATCHLIST_SORT_MODE


def normalize_progress_sort_mode(value: str | None, fallback: str | None = None) -> str:
    aliases = {
        canonical.casefold(): canonical
        for canonical, _label in PROGRESS_SORT_OPTIONS
    }
    aliases.update(
        {
            label.casefold(): canonical
            for canonical, label in PROGRESS_SORT_OPTIONS
        }
    )
    normalized = aliases.get((value or "").strip().casefold())
    if normalized is not None:
        return normalized
    fallback_normalized = aliases.get((fallback or "").strip().casefold())
    return fallback_normalized or DEFAULT_PROGRESS_SORT_MODE


def normalize_progress_sort_direction(value: str | None, fallback: str | None = None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"asc", "desc"}:
        return normalized
    fallback_normalized = (fallback or "").strip().lower()
    return fallback_normalized if fallback_normalized in {"asc", "desc"} else DEFAULT_PROGRESS_SORT_DIRECTION


def sort_watchlist_results(results: list[TitleSummary], mode: str, *, descending: bool = True) -> list[TitleSummary]:
    normalized_mode = normalize_watchlist_sort_mode(mode)
    if normalized_mode == "Alphabetical":
        return sorted(
            results,
            key=lambda item: ((item.title or "").casefold(), item.year or 0),
            reverse=descending,
        )
    if normalized_mode == "IMDb rating":
        return sorted(
            results,
            key=lambda item: (item.imdb_rating is not None, item.imdb_rating or 0.0, item.imdb_votes or 0),
            reverse=descending,
        )
    attribute = "released_at" if normalized_mode == "Release date" else "watchlisted_at"
    return sorted(
        results,
        key=lambda item: normalize_datetime(getattr(item, attribute, None)) or datetime.min.replace(tzinfo=UTC),
        reverse=descending,
    )


def parse_bool_flag(value: str | None, default: bool = False) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ratings_refresh_due(
    status: str | None,
    refreshed_at: datetime | None,
    *,
    asset_kind: str = ASSET_KIND_TITLE_RATINGS,
    ready_ttl_seconds: int,
    empty_ttl_seconds: int = RATINGS_EMPTY_REFRESH_SECONDS,
    now: datetime | None = None,
) -> bool:
    if asset_kind == ASSET_KIND_TITLE_RATINGS and ready_ttl_seconds != TITLE_RATINGS_READY_REFRESH_SECONDS:
        pass
    if asset_kind == ASSET_KIND_EPISODE_RATINGS and ready_ttl_seconds != EPISODE_RATINGS_READY_REFRESH_SECONDS:
        pass
    decision = metadata_refresh_due(
        asset_kind,
        status=status,
        last_checked_at=refreshed_at,
        has_value=(status or "").strip().lower() == "ready",
        trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
        now=now,
    )
    if (status or "").strip().lower() == "checked_no_data" and empty_ttl_seconds != RATINGS_EMPTY_REFRESH_SECONDS:
        refreshed = normalize_datetime(refreshed_at)
        if refreshed is None:
            return True
        now_value = normalize_datetime(now) or datetime.now(tz=UTC)
        return (now_value - refreshed) >= timedelta(seconds=empty_ttl_seconds)
    return decision.should_refresh


def progress_has_released_next_episode(item) -> bool:
    next_episode = getattr(item, "next_episode", None)
    if next_episode is None or getattr(next_episode, "first_aired", None) is None:
        return False
    release_at = next_episode.first_aired
    if release_at.tzinfo is None:
        release_at = release_at.replace(tzinfo=UTC)
    return release_at <= datetime.now(tz=UTC)


def progress_effective_aired(item) -> int:
    aired = int(getattr(item, "aired", 0) or 0)
    completed = int(getattr(item, "completed", 0) or 0)
    if progress_has_released_next_episode(item):
        return max(aired, completed + 1)
    next_episode = getattr(item, "next_episode", None)
    if next_episode is not None and getattr(next_episode, "first_aired", None) is not None:
        return min(aired, completed)
    return aired


def progress_effective_percent(item) -> float:
    completed = float(getattr(item, "completed", 0) or 0)
    aired = progress_effective_aired(item)
    if aired <= 0:
        return 0.0
    return (completed / aired) * 100.0


def filter_progress_items(
    items: list,
    *,
    hide_upcoming: bool,
    show_dropped: bool,
    show_paused: bool = False,
) -> list:
    filtered = items
    if show_dropped or show_paused:
        filtered = items
    elif hide_upcoming:
        filtered = [item for item in items if int(getattr(item, "completed", 0) or 0) < progress_effective_aired(item)]
    return filtered


def progress_skipped_count(item) -> int:
    return max(progress_effective_aired(item) - int(getattr(item, "completed", 0) or 0), 0)


def progress_rating_chip(item, rating_with_votes, primary_source: str | None = None) -> str:
    parts: list[str] = []
    primary_source = primary_source if primary_source in {"tmdb", "trakt"} else (
        "tmdb" if getattr(item, "provider", "trakt") == "tmdb" else "trakt"
    )
    primary_rating = getattr(item, "title_tmdb_rating", None) if primary_source == "tmdb" else getattr(item, "title_trakt_rating", None)
    primary_votes = getattr(item, "title_tmdb_votes", None) if primary_source == "tmdb" else getattr(item, "title_trakt_votes", None)
    primary_status = getattr(item, "title_ratings_status", "unknown")
    if primary_rating is not None and primary_votes is not None:
        primary_text = rating_with_votes(primary_rating, primary_votes)
    elif primary_status == "checked_no_data":
        primary_text = "n/a"
    else:
        primary_text = "Loading"
    imdb_status = getattr(item, "title_ratings_status", "unknown")
    if getattr(item, "title_imdb_rating", None) is not None and getattr(item, "title_imdb_votes", None) is not None:
        imdb_text = rating_with_votes(getattr(item, "title_imdb_rating", None), getattr(item, "title_imdb_votes", None))
    elif imdb_status == "checked_no_data":
        imdb_text = "n/a"
    else:
        imdb_text = "Loading"
    parts.append(f"{primary_source}|{primary_text}")
    parts.append(f"imdb|{imdb_text}")
    return " | ".join(parts)


def progress_episode_rating_chip(item, rating_with_votes, primary_source: str | None = None) -> str:
    next_episode = getattr(item, "next_episode", None)
    if next_episode is None:
        return ""
    parts: list[str] = []
    primary_source = primary_source if primary_source in {"tmdb", "trakt"} else (
        "tmdb" if getattr(item, "provider", "trakt") == "tmdb" else "trakt"
    )
    primary_rating = getattr(next_episode, "tmdb_rating", None) if primary_source == "tmdb" else getattr(next_episode, "trakt_rating", None)
    primary_votes = getattr(next_episode, "tmdb_votes", None) if primary_source == "tmdb" else getattr(next_episode, "trakt_votes", None)
    primary_status = getattr(next_episode, "trakt_details_status", "unknown")
    if primary_rating is not None and primary_votes is not None:
        primary_text = rating_with_votes(primary_rating, primary_votes)
    elif primary_status == "checked_no_data":
        primary_text = "n/a"
    else:
        primary_text = "Loading"
    imdb_status = getattr(next_episode, "imdb_status", "unknown")
    if getattr(next_episode, "imdb_rating", None) is not None and getattr(next_episode, "imdb_votes", None) is not None:
        imdb_text = rating_with_votes(getattr(next_episode, "imdb_rating", None), getattr(next_episode, "imdb_votes", None))
    elif imdb_status == "checked_no_data":
        imdb_text = "n/a"
    else:
        imdb_text = "Loading"
    parts.append(f"{primary_source}|{primary_text}")
    parts.append(f"imdb|{imdb_text}")
    return " | ".join(parts)


def progress_recent_release(item, *, hours: int = 48) -> bool:
    next_episode = getattr(item, "next_episode", None)
    if next_episode is None or getattr(next_episode, "first_aired", None) is None:
        return False
    release_at = next_episode.first_aired
    if release_at.tzinfo is None:
        release_at = release_at.replace(tzinfo=UTC)
    now = datetime.now(tz=UTC)
    return release_at <= now <= (release_at + timedelta(hours=hours))


def progress_query_string(
    *,
    hide_upcoming: bool,
    show_dropped: bool,
    show_paused: bool = False,
    sort_mode: str = DEFAULT_PROGRESS_SORT_MODE,
    sort_direction: str = DEFAULT_PROGRESS_SORT_DIRECTION,
    flash: str = "",
    rate_provider: str = "trakt",
    rate_trakt_id: int | None = None,
    rate_tmdb_id: int | None = None,
    rate_season: int | None = None,
    rate_episode: int | None = None,
    rate_title: str = "",
) -> str:
    params = {
        "hide_upcoming": "1" if hide_upcoming else "0",
        "show_paused": "1" if show_paused else "0",
        "show_dropped": "1" if show_dropped else "0",
        "sort": normalize_progress_sort_mode(sort_mode),
        "direction": normalize_progress_sort_direction(sort_direction),
    }
    if flash:
        params["flash"] = flash
    if rate_provider and rate_provider != "trakt":
        params["rate_provider"] = rate_provider
    if rate_trakt_id is not None:
        params["rate_trakt_id"] = str(rate_trakt_id)
    if rate_tmdb_id is not None:
        params["rate_tmdb_id"] = str(rate_tmdb_id)
    if rate_season is not None:
        params["rate_season"] = str(rate_season)
    if rate_episode is not None:
        params["rate_episode"] = str(rate_episode)
    if rate_title:
        params["rate_title"] = rate_title
    return urlencode(params)
def format_release_distance(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "Release date unknown"
    target = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    days = max(0, (target.date() - current.astimezone(UTC).date()).days)
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 28:
        return f"{days / 7:.1f} weeks"
    if days <= 183:
        return f"{days / 30.44:.1f} months"
    return str(target.year)
