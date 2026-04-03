from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from trakt_tracker.domain import TitleSummary


SEARCH_SORT_MODES = ("IMDb votes", "Trakt votes", "Alphabetical")
DEFAULT_SEARCH_SORT_MODE = "IMDb votes"
HISTORY_PAGE_SIZE = 50


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


def parse_bool_flag(value: str | None, default: bool = False) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def parse_progress_year(value: str | None) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        year = int(normalized)
    except ValueError:
        return None
    return year if 1900 <= year <= 3000 else None


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
    min_year: int | None = None,
    use_year_filter: bool = False,
) -> list:
    filtered = items
    if show_dropped:
        filtered = items
    elif hide_upcoming:
        filtered = [item for item in items if int(getattr(item, "completed", 0) or 0) < progress_effective_aired(item)]
    if use_year_filter and min_year is not None:
        filtered = [
            item
            for item in filtered
            if getattr(getattr(item, "next_episode", None), "first_aired", None) is not None
            and getattr(item.next_episode.first_aired, "year", None) is not None
            and item.next_episode.first_aired.year >= min_year
        ]
    return filtered


def progress_skipped_count(item) -> int:
    return max(progress_effective_aired(item) - int(getattr(item, "completed", 0) or 0), 0)


def progress_rating_chip(item, rating_with_votes) -> str:
    next_episode = getattr(item, "next_episode", None)
    if next_episode is None:
        return ""
    parts: list[str] = []
    trakt_status = getattr(next_episode, "trakt_details_status", "unknown")
    if getattr(next_episode, "trakt_rating", None) is not None and getattr(next_episode, "trakt_votes", None) is not None:
        trakt_text = rating_with_votes(getattr(next_episode, "trakt_rating", None), getattr(next_episode, "trakt_votes", None))
    elif trakt_status == "checked_no_data":
        trakt_text = "n/a"
    else:
        trakt_text = "Loading"
    imdb_status = getattr(next_episode, "imdb_status", "unknown")
    if getattr(next_episode, "imdb_rating", None) is not None and getattr(next_episode, "imdb_votes", None) is not None:
        imdb_text = rating_with_votes(getattr(next_episode, "imdb_rating", None), getattr(next_episode, "imdb_votes", None))
    elif imdb_status == "checked_no_data":
        imdb_text = "n/a"
    else:
        imdb_text = "Loading"
    parts.append(f"trakt|{trakt_text}")
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
    min_year: int | None = None,
    use_year_filter: bool = False,
    flash: str = "",
    rate_trakt_id: int | None = None,
    rate_season: int | None = None,
    rate_episode: int | None = None,
    rate_title: str = "",
) -> str:
    params = {
        "hide_upcoming": "1" if hide_upcoming else "0",
        "show_dropped": "1" if show_dropped else "0",
        "use_year_filter": "1" if use_year_filter else "0",
    }
    if min_year is not None:
        params["min_year"] = str(min_year)
    if flash:
        params["flash"] = flash
    if rate_trakt_id is not None:
        params["rate_trakt_id"] = str(rate_trakt_id)
    if rate_season is not None:
        params["rate_season"] = str(rate_season)
    if rate_episode is not None:
        params["rate_episode"] = str(rate_episode)
    if rate_title:
        params["rate_title"] = rate_title
    return urlencode(params)
