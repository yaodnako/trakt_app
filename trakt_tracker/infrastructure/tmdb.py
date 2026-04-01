from __future__ import annotations

from typing import Any

import httpx

from trakt_tracker.domain import TitleSummary
from trakt_tracker.infrastructure.cache import ProviderCache


TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"


class TMDbClient:
    def __init__(
        self,
        api_key: str = "",
        read_access_token: str = "",
        *,
        timeout: float = 20.0,
        cache_ttl_hours: int = 24,
    ) -> None:
        self.api_key = api_key.strip()
        self.read_access_token = read_access_token.strip()
        self._client = httpx.Client(timeout=timeout)
        self._cache = ProviderCache("tmdb")
        self._cache_ttl_hours = cache_ttl_hours

    def is_configured(self) -> bool:
        return bool(self.api_key or self.read_access_token)

    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        if not self.is_configured() or not title.tmdb_id:
            return title
        media_paths = ["tv", "movie"] if title.title_type == "show" else ["movie", "tv"]
        payload: dict[str, Any] | None = None
        for media_path in media_paths:
            payload = self._request_optional(
                "GET",
                f"/{media_path}/{title.tmdb_id}",
                params={"append_to_response": "external_ids"},
            )
            if isinstance(payload, dict):
                break
        if not isinstance(payload, dict):
            return title
        poster_path = payload.get("poster_path")
        if isinstance(poster_path, str) and poster_path:
            title.poster_url = f"{TMDB_IMAGE_BASE}{poster_path}"
        vote_average = payload.get("vote_average")
        if vote_average is not None:
            try:
                title.tmdb_rating = float(vote_average)
            except (TypeError, ValueError):
                title.tmdb_rating = None
        vote_count = payload.get("vote_count")
        if vote_count is not None:
            try:
                title.tmdb_votes = int(vote_count)
            except (TypeError, ValueError):
                title.tmdb_votes = None
        external_ids = payload.get("external_ids", {})
        if isinstance(external_ids, dict):
            imdb_id = external_ids.get("imdb_id")
            if isinstance(imdb_id, str) and imdb_id:
                title.imdb_id = imdb_id
        return title

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        headers = {
            "Accept": "application/json",
        }
        params = dict(params or {})
        if self.read_access_token:
            headers["Authorization"] = f"Bearer {self.read_access_token}"
        elif self.api_key:
            params["api_key"] = self.api_key
        cache_key = f"{method.upper()}|{path}|{repr(sorted(params.items()))}"
        cached = self._cache.get_json(cache_key, self._cache_ttl_hours)
        if cached is not None:
            return cached
        response = self._client.request(method, f"{TMDB_API_URL}{path}", headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
        self._cache.set_json(cache_key, payload)
        return payload

    def _request_optional(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any | None:
        try:
            return self._request(method, path, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
