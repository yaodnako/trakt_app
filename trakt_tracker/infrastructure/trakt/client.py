from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from trakt_tracker.domain import CalendarEntry, EpisodeSummary, HistoryItemInput, ProgressSnapshot, RatingInput, TitleSummary
from trakt_tracker.infrastructure.cache import ProviderCache
from trakt_tracker.infrastructure.keyring_store import TokenBundle
from trakt_tracker.infrastructure.url_utils import normalize_external_url


API_URL = "https://api.trakt.tv"


class TraktError(RuntimeError):
    pass


class TraktRateLimitError(TraktError):
    pass


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
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._client = httpx.Client(timeout=timeout)
        self._token: TokenBundle | None = None
        self._token_refresh_callback: Callable[[TokenBundle], None] | None = None
        self._cache = ProviderCache("trakt")
        self._cache_ttl_hours = cache_ttl_hours
        self._cache_namespace = cache_namespace

    def set_tokens(self, token: TokenBundle | None) -> None:
        self._token = token

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
        return self._request("GET", "/users/settings")

    def search_titles(self, query: str, title_type: str | None = None) -> list[TitleSummary]:
        endpoint = f"/search/{title_type}" if title_type else "/search/movie,show"
        data = self._request("GET", endpoint, params={"query": query, "extended": "full"})
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
                )
            )
        return [item for item in result if item.trakt_id]

    def get_title_details(self, trakt_id: int, title_type: str) -> TitleSummary:
        payload = self._request("GET", f"/{title_type}s/{trakt_id}", params={"extended": "full"})
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
            title=payload.get("title", f"Show {trakt_id}"),
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

    def get_episode_details(self, show_trakt_id: int, season: int, episode: int) -> EpisodeSummary | None:
        payload = self._request("GET", f"/shows/{show_trakt_id}/seasons/{season}/episodes/{episode}", params={"extended": "full"})
        if not isinstance(payload, dict):
            return None
        return self._parse_episode(payload, season)

    def get_watch_history(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        endpoint = "/sync/history"
        if title_type:
            endpoint = f"/sync/history/{title_type}s"
        return self._request("GET", endpoint, params={"limit": limit, "page": page, "extended": "full"})

    def get_ratings(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        endpoint = "/sync/ratings"
        if title_type:
            endpoint = f"/sync/ratings/{title_type}s"
        return self._request("GET", endpoint, params={"limit": limit, "page": page, "extended": "full"})

    def get_dropped_shows(self, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        return self._request("GET", "/users/hidden/dropped", params={"limit": limit, "page": page})

    def get_last_activities(self, *, use_cache: bool = True) -> dict[str, Any]:
        payload = self._request("GET", "/sync/last_activities", use_cache=use_cache)
        return payload if isinstance(payload, dict) else {}

    def add_history_item(self, item: HistoryItemInput) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        watched_at = item.watched_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if item.title_type == "movie":
            payload["movies"] = [{"ids": {"trakt": item.trakt_id}, "watched_at": watched_at}]
        elif item.season is not None and item.episode is not None:
            payload["episodes"] = [{"ids": {"trakt": item.trakt_id}, "watched_at": watched_at}]
        else:
            payload["shows"] = [{"ids": {"trakt": item.trakt_id}, "watched_at": watched_at}]
        return self._request("POST", "/sync/history", json=payload)

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
        _retry_on_401: bool = True,
        _retry_on_transport: bool = True,
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
        if method.upper() == "GET" and use_cache:
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
                    _retry_on_401=_retry_on_401,
                    _retry_on_transport=False,
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
                _retry_on_401=False,
                _retry_on_transport=_retry_on_transport,
                **kwargs,
            )
        if response.status_code == 429:
            raise TraktRateLimitError("Trakt rate limit exceeded")
        if response.status_code >= 400:
            raise TraktError(f"Trakt request failed: {response.status_code} {response.text}")
        if response.status_code == 204:
            return {}
        payload = response.json()
        if method.upper() == "GET" and use_cache:
            self._cache.set_json(cache_key, payload)
        return payload

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
