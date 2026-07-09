from __future__ import annotations

import json
import os
import subprocess
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import httpx

from trakt_tracker.domain import TitleSummary
from trakt_tracker.infrastructure.cache import ProviderCache


TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TMDB_BACKDROP_IMAGE_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_STILL_IMAGE_BASE = "https://image.tmdb.org/t/p/w780"


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
            title.poster_url = f"{TMDB_POSTER_IMAGE_BASE}{poster_path}"
        backdrop_path = payload.get("backdrop_path")
        if isinstance(backdrop_path, str) and backdrop_path:
            title.backdrop_url = f"{TMDB_BACKDROP_IMAGE_BASE}{backdrop_path}"
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

    def get_episode_still_url(self, show_tmdb_id: int, season: int, episode: int) -> str:
        if not self.is_configured() or not show_tmdb_id:
            return ""
        payload = self._request_optional(
            "GET",
            f"/tv/{show_tmdb_id}/season/{season}/episode/{episode}",
        )
        if not isinstance(payload, dict):
            return ""
        still_path = payload.get("still_path")
        if isinstance(still_path, str) and still_path:
            return f"{TMDB_STILL_IMAGE_BASE}{still_path}"
        return ""

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
        try:
            response = self._client.request(method, f"{TMDB_API_URL}{path}", headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise
            payload = self._request_with_curl(method, path, headers=headers, params=params)
        except (httpx.HTTPError, ValueError):
            payload = self._request_with_curl(method, path, headers=headers, params=params)
        self._cache.set_json(cache_key, payload)
        return payload

    def _request_with_curl(self, method: str, path: str, *, headers: dict[str, str], params: dict[str, Any]) -> Any:
        query = urlencode(params)
        url = f"{TMDB_API_URL}{path}"
        if query:
            url = f"{url}?{query}"
        command = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--ipv4",
            "--doh-url",
            "https://cloudflare-dns.com/dns-query",
            "--max-time",
            str(int(self._client.timeout.connect or 20.0)),
            "--request",
            method.upper(),
            "--header",
            "Accept: application/json",
        ]
        authorization = headers.get("Authorization")
        if authorization:
            command.extend(["--header", f"Authorization: {authorization}"])
        command.append(url)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=(self._client.timeout.connect or 20.0) + 2.0,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            return json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return self._request_with_urllib(method, path, headers=headers, params=params)

    def _request_with_urllib(self, method: str, path: str, *, headers: dict[str, str], params: dict[str, Any]) -> Any:
        query = urlencode(params)
        url = f"{TMDB_API_URL}{path}"
        if query:
            url = f"{url}?{query}"
        request = UrlRequest(url, headers=headers, method=method.upper())
        with urlopen(request, timeout=self._client.timeout.connect or 20.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def _request_optional(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any | None:
        try:
            return self._request(method, path, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
