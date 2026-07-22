from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Any, Callable

import httpx

from trakt_tracker.domain import (
    CalendarEntry,
    EpisodeSummary,
    ExploreResultPage,
    HistoryItemInput,
    ProgressSnapshot,
    RatingInput,
    TitleSummary,
)
from trakt_tracker.infrastructure.cache import ProviderCache
from trakt_tracker.infrastructure.keyring_store import TokenBundle
from trakt_tracker.infrastructure.url_utils import normalize_external_url


API_URL = "https://api.trakt.tv"


class TraktError(RuntimeError):
    pass


class TraktRateLimitError(TraktError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    created_at: int
    expires_in: int
    token_type: str
    scope: str = ""

    def to_bundle(self) -> TokenBundle:
        return TokenBundle(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            created_at=self.created_at,
            expires_in=self.expires_in,
            token_type=self.token_type,
            scope=self.scope,
        )


class TraktClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        timeout: float = 20.0,
        cache_ttl_hours: int = 24,
        cache_namespace: str = "default",
        cache_provider: str = "trakt",
        rate_limit_sleep: Callable[[float], None] = time.sleep,
        rate_limit_jitter: Callable[[], float] = random.random,
        rate_limit_wait_budget_seconds: float = 300.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._client = httpx.Client(timeout=timeout)
        self._token: TokenBundle | None = None
        self._token_refresh_callback: Callable[[TokenBundle], None] | None = None
        self._cache = ProviderCache(cache_provider)
        self._cache_ttl_hours = cache_ttl_hours
        self._cache_namespace = cache_namespace
        self._rate_limit_sleep = rate_limit_sleep
        self._rate_limit_jitter = rate_limit_jitter
        self._rate_limit_wait_budget_seconds = max(0.0, float(rate_limit_wait_budget_seconds))

    def set_tokens(self, token: TokenBundle | None) -> None:
        self._token = token

    def close(self) -> None:
        """Release the persistent HTTP connection pool owned by this client."""
        self._client.close()

    def set_token_refresh_callback(self, callback: Callable[[TokenBundle], None] | None) -> None:
        self._token_refresh_callback = callback

    def clear_cache(self) -> None:
        self._cache.clear()

    def exchange_code(self, code: str) -> OAuthTokens:
        payload = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        data = self._request("POST", "/oauth/token", auth_required=False, json=payload)
        return OAuthTokens(**data)

    def refresh_tokens(self) -> OAuthTokens:
        if not self._token:
            raise TraktError("Refresh token is not configured")
        payload = {
            "refresh_token": self._token.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "refresh_token",
        }
        data = self._request("POST", "/oauth/token", auth_required=False, json=payload)
        return OAuthTokens(**data)

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/users/settings", use_cache=False)

    def search_titles(self, query: str, title_type: str | None = None) -> list[TitleSummary]:
        return self.get_search_titles_page(query, title_type, page=1, limit=100).items

    def get_search_titles_page(
        self,
        query: str,
        title_type: str | None = None,
        *,
        page: int = 1,
        limit: int = 24,
    ) -> ExploreResultPage:
        endpoint = f"/search/{title_type}" if title_type else "/search/movie,show"
        params = {
            "query": query,
            "extended": "full",
            "page": max(1, page),
            "limit": max(1, min(100, limit)),
        }
        data, response_headers = self._request(
            "GET",
            endpoint,
            params=params,
            use_cache=False,
            include_headers=True,
        )
        result: list[TitleSummary] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if not isinstance(item_type, str):
                continue
            payload = item.get(item_type, {})
            if not isinstance(payload, dict):
                continue
            ids = payload.get("ids", {})
            if not isinstance(ids, dict):
                ids = {}
            result.append(
                TitleSummary(
                    trakt_id=ids.get("trakt"),
                    title_type=item_type,
                    title=payload.get("title", ""),
                    year=payload.get("year"),
                    overview=payload.get("overview", ""),
                    poster_url=self._extract_poster_url(payload),
                    status=payload.get("status", ""),
                    slug=ids.get("slug", ""),
                    trakt_rating=self._as_float(payload.get("rating")),
                    trakt_votes=self._as_int(payload.get("votes")),
                    tmdb_id=self._as_int(ids.get("tmdb")),
                    imdb_id=str(ids.get("imdb", "") or ""),
                    imdb_rating=self._extract_imdb_rating(payload),
                    released_at=self._parse_optional_datetime(payload.get("released") or payload.get("first_aired")),
                )
            )
        current_page = self._as_int(response_headers.get("x-pagination-page")) or max(1, page)
        page_count = self._as_int(response_headers.get("x-pagination-page-count")) or current_page
        return ExploreResultPage(
            items=[item for item in result if item.trakt_id],
            page=current_page,
            page_count=max(current_page, page_count),
        )

    def get_explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int = 1,
        limit: int = 24,
        trakt_min: float | None = None,
    ) -> ExploreResultPage:
        if title_type not in {"movie", "show"}:
            raise ValueError("Unsupported Explore title type")
        if feed not in {"anticipated", "trending", "popular"}:
            raise ValueError("Unsupported Explore feed")
        params = {"page": max(1, page), "limit": max(1, min(100, limit)), "extended": "full"}
        if trakt_min is not None and feed != "anticipated":
            minimum_percent = max(0, min(100, int(float(trakt_min) * 10 + 0.999999)))
            params["ratings"] = f"{minimum_percent}-100"
        payload, response_headers = self._request(
            "GET",
            f"/{title_type}s/{feed}",
            params=params,
            use_cache=False,
            include_headers=True,
        )
        rows = payload if isinstance(payload, list) else []
        items: list[TitleSummary] = []
        now = datetime.now(tz=UTC)
        for row in rows:
            if not isinstance(row, dict):
                continue
            title_payload = row.get(title_type) if feed in {"anticipated", "trending"} else row
            if not isinstance(title_payload, dict):
                continue
            ids = title_payload.get("ids", {})
            if not isinstance(ids, dict) or not ids.get("trakt"):
                continue
            released_at = self._parse_optional_datetime(
                title_payload.get("released") or title_payload.get("first_aired")
            )
            metric_kind = "watching" if feed == "trending" else ("lists" if feed == "anticipated" else "")
            metric_count = self._as_int(
                row.get("watchers") if feed == "trending" else row.get("list_count")
            ) if metric_kind else None
            actions_available = True
            if feed == "anticipated":
                actions_available = released_at is not None and (
                    released_at.replace(tzinfo=UTC) if released_at.tzinfo is None else released_at.astimezone(UTC)
                ) <= now
            items.append(
                TitleSummary(
                    trakt_id=ids["trakt"],
                    title_type=title_type,
                    title=title_payload.get("title", ""),
                    year=title_payload.get("year"),
                    overview=title_payload.get("overview", ""),
                    poster_url=self._extract_poster_url(title_payload),
                    status=title_payload.get("status", ""),
                    slug=ids.get("slug", ""),
                    trakt_rating=self._as_float(title_payload.get("rating")),
                    trakt_votes=self._as_int(title_payload.get("votes")),
                    tmdb_id=self._as_int(ids.get("tmdb")),
                    imdb_id=str(ids.get("imdb", "") or ""),
                    imdb_rating=self._extract_imdb_rating(title_payload),
                    released_at=released_at,
                    explore_metric_kind=metric_kind,
                    explore_metric_count=metric_count,
                    catalog_actions_available=actions_available,
                )
            )
        current_page = self._as_int(response_headers.get("x-pagination-page")) or max(1, page)
        page_count = self._as_int(response_headers.get("x-pagination-page-count")) or current_page
        return ExploreResultPage(items=items, page=current_page, page_count=max(current_page, page_count))

    def get_title_details(self, trakt_id: int, title_type: str, *, use_cache: bool = True) -> TitleSummary:
        payload = self._request("GET", f"/{title_type}s/{trakt_id}", params={"extended": "full"}, use_cache=use_cache)
        if not isinstance(payload, dict):
            raise TraktError("Unexpected Trakt title details payload")
        ids = payload.get("ids", {})
        if not isinstance(ids, dict):
            ids = {}
        return TitleSummary(
            trakt_id=ids.get("trakt", trakt_id),
            title_type=title_type,
            title=payload.get("title", ""),
            year=payload.get("year"),
            overview=payload.get("overview", ""),
            poster_url=self._extract_poster_url(payload),
            status=payload.get("status", ""),
            slug=ids.get("slug", ""),
            trakt_rating=self._as_float(payload.get("rating")),
            trakt_votes=self._as_int(payload.get("votes")),
            tmdb_id=self._as_int(ids.get("tmdb")),
            imdb_id=str(ids.get("imdb", "") or ""),
            imdb_rating=self._extract_imdb_rating(payload),
        )

    def get_show_progress(self, trakt_id: int, *, use_cache: bool = True) -> ProgressSnapshot:
        payload = self._request("GET", f"/shows/{trakt_id}/progress/watched", use_cache=use_cache)
        return ProgressSnapshot(
            trakt_id=trakt_id,
            title=str(payload.get("title", "") or ""),
            completed=payload.get("completed", 0),
            aired=payload.get("aired", 0),
            percent_completed=float(payload.get("completed", 0)) / max(payload.get("aired", 1), 1) * 100.0,
            next_episode=self._parse_episode(payload.get("next_episode")),
            last_episode=self._parse_episode(payload.get("last_episode")),
        )

    def get_show_episodes(self, trakt_id: int) -> list[EpisodeSummary]:
        payload = self._request("GET", f"/shows/{trakt_id}/seasons", params={"extended": "episodes,full"})
        episodes: list[EpisodeSummary] = []
        for season in payload:
            season_number = season.get("number", 0)
            for episode in season.get("episodes", []):
                parsed = self._parse_episode(episode, season_number)
                if parsed is not None:
                    episodes.append(parsed)
        return episodes

    def get_episode_details(self, show_trakt_id: int, season: int, episode: int, *, use_cache: bool = True) -> EpisodeSummary | None:
        payload = self._request(
            "GET",
            f"/shows/{show_trakt_id}/seasons/{season}/episodes/{episode}",
            params={"extended": "full"},
            use_cache=use_cache,
        )
        if not isinstance(payload, dict):
            return None
        return self._parse_episode(payload, season)

    def get_watch_history(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        endpoint = "/sync/history"
        if title_type:
            endpoint = f"/sync/history/{title_type}s"
        return self._request("GET", endpoint, params={"limit": limit, "page": page, "extended": "full"})

    def get_watch_history_page(
        self,
        title_type: str | None = None,
        limit: int = 1000,
        page: int = 1,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        endpoint = "/sync/history"
        if title_type:
            endpoint = f"/sync/history/{title_type}s"
        payload, headers = self._request(
            "GET",
            endpoint,
            params={"limit": limit, "page": page, "extended": "full"},
            use_cache=False,
            include_headers=True,
        )
        return (payload if isinstance(payload, list) else []), headers

    def get_ratings(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        endpoint = "/sync/ratings"
        if title_type:
            endpoint = f"/sync/ratings/{title_type}s"
        return self._request("GET", endpoint, params={"limit": limit, "page": page, "extended": "full"})

    def get_watchlist(self) -> list[TitleSummary]:
        data = []
        for item_type in ("movies", "shows"):
            payload = self._request(
                "GET",
                f"/sync/watchlist/{item_type}",
                params={"extended": "full", "limit": 1000},
                use_cache=False,
            )
            if isinstance(payload, list):
                data.extend(payload)
        result: list[TitleSummary] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "") or "")
            payload = item.get(item_type, {})
            if item_type not in {"movie", "show"} or not isinstance(payload, dict):
                continue
            ids = payload.get("ids", {})
            if not isinstance(ids, dict) or not ids.get("trakt"):
                continue
            result.append(
                TitleSummary(
                    trakt_id=ids["trakt"],
                    title_type=item_type,
                    title=payload.get("title", ""),
                    year=payload.get("year"),
                    overview=payload.get("overview", ""),
                    poster_url=self._extract_poster_url(payload),
                    status=payload.get("status", ""),
                    slug=ids.get("slug", ""),
                    trakt_rating=self._as_float(payload.get("rating")),
                    trakt_votes=self._as_int(payload.get("votes")),
                    tmdb_id=self._as_int(ids.get("tmdb")),
                    imdb_id=str(ids.get("imdb", "") or ""),
                    imdb_rating=self._extract_imdb_rating(payload),
                    is_watchlisted=True,
                    watchlisted_at=self._parse_optional_datetime(item.get("listed_at")),
                    released_at=self._parse_optional_datetime(payload.get("released") or payload.get("first_aired")),
                )
            )
        return result

    def set_watchlist(self, title_type: str, trakt_id: int, *, watchlisted: bool) -> dict[str, Any]:
        if title_type not in {"movie", "show"}:
            raise ValueError("Unsupported title type")
        key = "movies" if title_type == "movie" else "shows"
        endpoint = "/sync/watchlist" if watchlisted else "/sync/watchlist/remove"
        return self._request("POST", endpoint, json={key: [{"ids": {"trakt": trakt_id}}]})

    def get_release_tracking(self) -> list[TitleSummary]:
        list_id = self._release_tracking_list_id()
        payload = self._request(
            "GET",
            f"/users/me/lists/{list_id}/items",
            params={"extended": "full", "limit": 1000},
            use_cache=False,
        )
        result: list[TitleSummary] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "") or "")
            title_payload = item.get(item_type, {})
            if item_type not in {"movie", "show"} or not isinstance(title_payload, dict):
                continue
            ids = title_payload.get("ids", {})
            if not isinstance(ids, dict) or not ids.get("trakt"):
                continue
            result.append(
                TitleSummary(
                    trakt_id=int(ids["trakt"]),
                    title_type=item_type,
                    title=str(title_payload.get("title", "") or ""),
                    year=title_payload.get("year"),
                    overview=str(title_payload.get("overview", "") or ""),
                    poster_url=self._extract_poster_url(title_payload),
                    status=str(title_payload.get("status", "") or ""),
                    slug=str(ids.get("slug", "") or ""),
                    trakt_rating=self._as_float(title_payload.get("rating")),
                    trakt_votes=self._as_int(title_payload.get("votes")),
                    tmdb_id=self._as_int(ids.get("tmdb")),
                    imdb_id=str(ids.get("imdb", "") or ""),
                    imdb_rating=self._extract_imdb_rating(title_payload),
                    released_at=self._parse_optional_datetime(title_payload.get("released") or title_payload.get("first_aired")),
                    is_release_tracked=True,
                )
            )
        return result

    def set_release_tracking(self, title_type: str, trakt_id: int, *, tracked: bool) -> dict[str, Any]:
        if title_type not in {"movie", "show"}:
            raise ValueError("Unsupported title type")
        list_id = self._release_tracking_list_id()
        key = "movies" if title_type == "movie" else "shows"
        suffix = "items" if tracked else "items/remove"
        return self._request(
            "POST",
            f"/users/me/lists/{list_id}/{suffix}",
            json={key: [{"ids": {"trakt": int(trakt_id)}}]},
        )

    def _release_tracking_list_id(self) -> str:
        payload = self._request("GET", "/users/me/lists", use_cache=False)
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict) or str(item.get("name", "") or "").strip().casefold() != "release_tracking":
                continue
            ids = item.get("ids", {})
            if isinstance(ids, dict):
                value = ids.get("trakt") or ids.get("slug")
                if value:
                    return str(value)
        raise TraktError('Trakt list "Release_tracking" was not found.')

    @staticmethod
    def _parse_optional_datetime(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    def get_dropped_shows(self, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        return self._request("GET", "/users/hidden/dropped", params={"limit": limit, "page": page})

    def get_last_activities(self, *, use_cache: bool = True) -> dict[str, Any]:
        payload = self._request("GET", "/sync/last_activities", use_cache=use_cache)
        return payload if isinstance(payload, dict) else {}

    def add_history_item(self, item: HistoryItemInput) -> dict[str, Any]:
        return self.add_history_items([item])

    def add_history_items(self, items: list[HistoryItemInput]) -> dict[str, Any]:
        payload = self._history_items_payload(items, include_watched_at=True)
        if not payload:
            return {}
        return self._request("POST", "/sync/history", json=payload)

    def remove_history_items(self, items: list[HistoryItemInput]) -> dict[str, Any]:
        payload = self._history_items_payload(items, include_watched_at=False)
        if not payload:
            return {}
        return self._request("POST", "/sync/history/remove", json=payload)

    @staticmethod
    def _history_items_payload(items: list[HistoryItemInput], *, include_watched_at: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for item in items:
            body: dict[str, Any] = {"ids": {"trakt": item.trakt_id}}
            if include_watched_at:
                if item.watched_at is None:
                    raise ValueError("Trakt history sync requires a watched date.")
                body["watched_at"] = item.watched_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if item.title_type == "movie":
                payload.setdefault("movies", []).append(body)
            elif item.season is not None and item.episode is not None:
                payload.setdefault("episodes", []).append(body)
            else:
                payload.setdefault("shows", []).append(body)
        return payload

    def set_rating(self, item: RatingInput) -> dict[str, Any]:
        if not 1 <= item.rating <= 10:
            raise ValueError("Rating must be between 1 and 10")
        payload: dict[str, Any] = {}
        body = {"ids": {"trakt": item.trakt_id}, "rating": item.rating}
        if item.title_type == "movie":
            payload["movies"] = [body]
        elif item.season is not None and item.episode is not None:
            payload["episodes"] = [body]
        else:
            payload["shows"] = [body]
        return self._request("POST", "/sync/ratings", json=payload)

    def get_calendar(self, start_date: str, days: int = 14) -> list[CalendarEntry]:
        payload = self._request("GET", f"/calendars/my/shows/{start_date}/{days}")
        entries: list[CalendarEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            show = item.get("show", {})
            episode = item.get("episode", {})
            if not isinstance(show, dict):
                continue
            parsed = self._parse_episode(episode)
            if parsed is None:
                continue
            ids = show.get("ids", {})
            if not isinstance(ids, dict):
                ids = {}
            entries.append(
                CalendarEntry(
                    show_trakt_id=ids.get("trakt"),
                    show_title=show.get("title", ""),
                    episode=parsed,
                )
            )
        return [entry for entry in entries if entry.show_trakt_id and entry.episode.trakt_id]

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth_required: bool = True,
        use_cache: bool = True,
        include_headers: bool = False,
        _retry_on_401: bool = True,
        _retry_on_transport: bool = True,
        _rate_limit_attempt: int = 0,
        _rate_limit_waited: float = 0.0,
        **kwargs: Any,
    ) -> Any:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-key": self.client_id,
            "trakt-api-version": "2",
        }
        if auth_required:
            if not self._token:
                raise TraktError("Authentication is required")
            headers["Authorization"] = f"Bearer {self._token.access_token}"
        if method.upper() == "GET" and use_cache and not include_headers:
            cache_key = self._make_cache_key(method, path, kwargs.get("params"), auth_required)
            cached = self._cache.get_json(cache_key, self._cache_ttl_hours)
            if cached is not None:
                return cached
        try:
            response = self._client.request(method, f"{API_URL}{path}", headers=headers, **kwargs)
        except httpx.TransportError as exc:
            if _retry_on_transport and method.upper() == "GET":
                return self._request(
                    method,
                    path,
                    auth_required=auth_required,
                    use_cache=use_cache,
                    include_headers=include_headers,
                    _retry_on_401=_retry_on_401,
                    _retry_on_transport=False,
                    _rate_limit_attempt=_rate_limit_attempt,
                    _rate_limit_waited=_rate_limit_waited,
                    **kwargs,
                )
            raise TraktError(str(exc)) from exc
        if response.status_code == 401 and auth_required and _retry_on_401 and self._token is not None:
            refreshed = self.refresh_tokens()
            bundle = refreshed.to_bundle()
            self.set_tokens(bundle)
            if self._token_refresh_callback is not None:
                self._token_refresh_callback(bundle)
            return self._request(
                method,
                path,
                auth_required=auth_required,
                use_cache=use_cache,
                include_headers=include_headers,
                _retry_on_401=False,
                _retry_on_transport=_retry_on_transport,
                _rate_limit_attempt=_rate_limit_attempt,
                _rate_limit_waited=_rate_limit_waited,
                **kwargs,
            )
        if response.status_code == 429:
            retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
            if method.upper() == "GET" and _rate_limit_attempt < 2:
                base_delay = float(retry_after if retry_after is not None else (5 if _rate_limit_attempt == 0 else 15))
                jitter = min(1.0, max(0.0, base_delay * 0.1)) * max(0.0, self._rate_limit_jitter())
                delay = base_delay + jitter
                remaining = self._rate_limit_wait_budget_seconds - _rate_limit_waited
                if delay <= remaining:
                    logging.getLogger(__name__).warning(
                        "Trakt rate limited endpoint=%s wait_seconds=%.1f attempt=%d",
                        path.split("?", 1)[0],
                        delay,
                        _rate_limit_attempt + 1,
                    )
                    self._rate_limit_sleep(delay)
                    return self._request(
                        method,
                        path,
                        auth_required=auth_required,
                        use_cache=use_cache,
                        include_headers=include_headers,
                        _retry_on_401=_retry_on_401,
                        _retry_on_transport=_retry_on_transport,
                        _rate_limit_attempt=_rate_limit_attempt + 1,
                        _rate_limit_waited=_rate_limit_waited + delay,
                        **kwargs,
                    )
            detail = "Trakt rate limit exceeded"
            if retry_after is not None:
                detail = f"{detail}; retry after {retry_after} seconds"
            raise TraktRateLimitError(detail, retry_after_seconds=retry_after)
        if response.status_code >= 400:
            raise TraktError(f"Trakt request failed: {response.status_code} {response.text}")
        if response.status_code == 204:
            return ({}, dict(response.headers)) if include_headers else {}
        payload = response.json()
        if method.upper() == "GET" and use_cache and not include_headers:
            self._cache.set_json(cache_key, payload)
        return (payload, dict(response.headers)) if include_headers else payload

    @staticmethod
    def _retry_after_seconds(value: str | None) -> int | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return max(0, ceil(float(raw)))
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0, ceil((target.astimezone(UTC) - datetime.now(tz=UTC)).total_seconds()))

    @staticmethod
    def _parse_episode(payload: dict[str, Any] | None, season_number: int | None = None) -> EpisodeSummary | None:
        if not payload or not isinstance(payload, dict):
            return None
        first_aired_raw = payload.get("first_aired")
        first_aired = None
        if first_aired_raw:
            first_aired = datetime.fromisoformat(first_aired_raw.replace("Z", "+00:00"))
        ids = payload.get("ids", {})
        if not isinstance(ids, dict):
            ids = {}
        return EpisodeSummary(
            trakt_id=ids.get("trakt", 0),
            season=payload.get("season", season_number or 0),
            number=payload.get("number", 0),
            title=payload.get("title", ""),
            trakt_rating=TraktClient._as_float(payload.get("rating")),
            trakt_votes=TraktClient._as_int(payload.get("votes")),
            imdb_id=str(ids.get("imdb", "") or ""),
            first_aired=first_aired,
            runtime=payload.get("runtime"),
            overview=payload.get("overview", ""),
        )

    @staticmethod
    def _extract_poster_url(payload: dict[str, Any]) -> str:
        images = payload.get("images", {})
        if not isinstance(images, dict):
            return ""
        poster = images.get("poster", [])
        if isinstance(poster, list) and poster:
            first = poster[0]
            if isinstance(first, dict):
                return normalize_external_url(str(first.get("url", "")))
            if isinstance(first, str):
                return normalize_external_url(first)
            return ""
        if isinstance(poster, dict):
            return normalize_external_url(str(poster.get("url", "")))
        if isinstance(poster, str):
            return normalize_external_url(poster)
        return ""

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_imdb_rating(payload: dict[str, Any]) -> float | None:
        external = payload.get("external_ratings")
        if isinstance(external, dict):
            imdb = external.get("imdb")
            if isinstance(imdb, dict):
                return TraktClient._as_float(imdb.get("rating"))
            return TraktClient._as_float(imdb)
        ratings = payload.get("ratings")
        if isinstance(ratings, dict):
            imdb = ratings.get("imdb")
            if isinstance(imdb, dict):
                return TraktClient._as_float(imdb.get("rating"))
            return TraktClient._as_float(imdb)
        return None

    def _make_cache_key(self, method: str, path: str, params: Any, auth_required: bool) -> str:
        params_repr = repr(sorted((params or {}).items())) if isinstance(params, dict) else repr(params)
        return f"{self._cache_namespace}|{auth_required}|{method.upper()}|{path}|{params_repr}"
