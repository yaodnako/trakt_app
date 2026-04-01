from __future__ import annotations

from typing import Any

import httpx

from trakt_tracker.infrastructure.cache import ProviderCache


KINOPOISK_API_URL = "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"
KINOPOISK_CACHE_TTL_HOURS = 24 * 30


def normalize_cache_key(title: str) -> str:
    return " ".join(title.strip().casefold().split())


def build_film_url(film_id: int, domain: str) -> str:
    normalized_domain = (domain or "ru").strip().casefold().strip(".")
    if normalized_domain not in {"ru", "net"}:
        normalized_domain = "ru"
    return f"https://www.kinopoisk.{normalized_domain}/film/{film_id}/"


def extract_first_film_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    films = payload.get("films")
    if not isinstance(films, list):
        return None
    for item in films:
        if not isinstance(item, dict):
            continue
        film_id = item.get("filmId")
        try:
            normalized = int(film_id)
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            return normalized
    return None


class KinopoiskClient:
    def __init__(self, api_key: str = "", *, timeout: float = 20.0, cache_ttl_hours: int = KINOPOISK_CACHE_TTL_HOURS) -> None:
        self.api_key = api_key.strip()
        self._client = httpx.Client(timeout=timeout)
        self._cache = ProviderCache("kinopoisk")
        self._cache_ttl_hours = cache_ttl_hours

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def resolve_title_url(self, title: str, domain: str = "net") -> str | None:
        normalized_title = title.strip()
        if not normalized_title:
            return None
        cache_key = normalize_cache_key(normalized_title)
        cached = self._cache.get_json(cache_key, self._cache_ttl_hours)
        film_id = self._extract_cached_film_id(cached)
        if film_id is None and self.is_configured():
            try:
                film_id = self._search_film_id(normalized_title)
            except (httpx.HTTPError, ValueError):
                film_id = None
            if film_id is not None:
                self._cache.set_json(cache_key, {"filmId": film_id})
        if film_id is None:
            return None
        return build_film_url(film_id, domain)

    def _search_film_id(self, title: str) -> int | None:
        response = self._client.get(
            KINOPOISK_API_URL,
            headers={
                "Accept": "application/json",
                "X-API-KEY": self.api_key,
            },
            params={"keyword": title, "page": 1},
        )
        response.raise_for_status()
        payload = response.json()
        return extract_first_film_id(payload)

    @staticmethod
    def _extract_cached_film_id(payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        try:
            film_id = int(payload.get("filmId"))
        except (TypeError, ValueError):
            return None
        return film_id if film_id > 0 else None
