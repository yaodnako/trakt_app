from __future__ import annotations

from typing import Any

import httpx

from trakt_tracker.domain import TitleSummary
from trakt_tracker.infrastructure.cache import ProviderCache


OMDB_API_URL = "https://www.omdbapi.com/"


class OMDbClient:
    def __init__(self, api_key: str = "", *, timeout: float = 20.0, cache_ttl_hours: int = 24) -> None:
        self.api_key = api_key.strip()
        self._client = httpx.Client(timeout=timeout)
        self._cache = ProviderCache("omdb")
        self._cache_ttl_hours = cache_ttl_hours

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        if not self.is_configured() or not title.imdb_id:
            return title
        payload = self._request(params={"apikey": self.api_key, "i": title.imdb_id})
        if not isinstance(payload, dict):
            return title
        if payload.get("Response") == "False":
            return title
        imdb_rating = payload.get("imdbRating")
        try:
            if imdb_rating and imdb_rating != "N/A":
                title.imdb_rating = float(imdb_rating)
        except (TypeError, ValueError):
            title.imdb_rating = None
        imdb_votes = payload.get("imdbVotes")
        try:
            if imdb_votes and imdb_votes != "N/A":
                title.imdb_votes = int(str(imdb_votes).replace(",", ""))
        except (TypeError, ValueError):
            title.imdb_votes = None
        poster = payload.get("Poster")
        if (not title.poster_url) and isinstance(poster, str) and poster and poster != "N/A":
            title.poster_url = poster
        return title

    def _request(self, *, params: dict[str, Any]) -> Any:
        cache_key = repr(sorted(params.items()))
        cached = self._cache.get_json(cache_key, self._cache_ttl_hours)
        if cached is not None:
            return cached
        response = self._client.get(OMDB_API_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        self._cache.set_json(cache_key, payload)
        return payload
