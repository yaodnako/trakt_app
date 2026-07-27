from __future__ import annotations

import json
import os
import socket
import subprocess
from ipaddress import ip_address
from time import monotonic
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import httpx

from trakt_tracker.domain import TitleSummary
from trakt_tracker.infrastructure.cache import ProviderCache
from trakt_tracker.infrastructure.fragmented_https import request_with_fragmented_tls


TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TMDB_BACKDROP_IMAGE_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_STILL_IMAGE_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_DIRECT_ATTEMPT_SECONDS = 3.0
TMDB_CURL_ATTEMPT_SECONDS = 4.0
TMDB_URLLIB_ATTEMPT_SECONDS = 2.0


class TMDbClient:
    def __init__(
        self,
        api_key: str = "",
        read_access_token: str = "",
        *,
        timeout: float = 15.0,
        cache_ttl_hours: int = 24,
    ) -> None:
        self.api_key = api_key.strip()
        self.read_access_token = read_access_token.strip()
        self._request_budget_seconds = max(1.0, float(timeout))
        self._client = httpx.Client(timeout=self._request_budget_seconds)
        self._cache = ProviderCache("tmdb")
        self._cache_ttl_hours = cache_ttl_hours
        self._direct_dns_loopback_only: bool | None = None

    def is_configured(self) -> bool:
        return bool(self.api_key or self.read_access_token)

    def close(self) -> None:
        """Release the persistent HTTP connection pool owned by this client."""
        self._client.close()

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

    def get_season_episode_still_urls(self, show_tmdb_id: int, season: int) -> dict[int, str]:
        """Resolve every episode still in a season with one TMDb request."""
        if not self.is_configured() or not show_tmdb_id:
            return {}
        payload = self._request_optional("GET", f"/tv/{show_tmdb_id}/season/{season}")
        if not isinstance(payload, dict):
            return {}
        result: dict[int, str] = {}
        for episode in payload.get("episodes", []):
            if not isinstance(episode, dict):
                continue
            number = episode.get("episode_number")
            still_path = episode.get("still_path")
            if isinstance(number, int) and isinstance(still_path, str) and still_path:
                result[number] = f"{TMDB_STILL_IMAGE_BASE}{still_path}"
        return result

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
        deadline = monotonic() + self._request_budget_seconds
        if self._system_dns_is_loopback_only():
            try:
                payload = self._request_with_fragmented_tls(
                    method,
                    path,
                    headers=headers,
                    params=params,
                    deadline=deadline,
                )
            except Exception:
                payload = self._request_with_curl(
                    method,
                    path,
                    headers=headers,
                    params=params,
                    deadline=deadline,
                )
        else:
            try:
                response = self._client.request(
                    method,
                    f"{TMDB_API_URL}{path}",
                    headers=headers,
                    params=params,
                    timeout=self._attempt_timeout(deadline, TMDB_DIRECT_ATTEMPT_SECONDS),
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise
                payload = self._request_with_curl(method, path, headers=headers, params=params, deadline=deadline)
            except (httpx.HTTPError, ValueError):
                payload = self._request_with_curl(method, path, headers=headers, params=params, deadline=deadline)
        self._cache.set_json(cache_key, payload)
        return payload

    def _system_dns_is_loopback_only(self) -> bool:
        if self._direct_dns_loopback_only is not None:
            return self._direct_dns_loopback_only
        try:
            addresses = {
                str(info[4][0]).split("%", 1)[0]
                for info in socket.getaddrinfo("api.themoviedb.org", 443, type=socket.SOCK_STREAM)
                if info[4]
            }
            parsed = [ip_address(address) for address in addresses]
        except (OSError, ValueError):
            self._direct_dns_loopback_only = False
        else:
            self._direct_dns_loopback_only = bool(parsed) and all(
                address.is_loopback or address.is_unspecified
                for address in parsed
            )
        return self._direct_dns_loopback_only

    def _request_with_curl(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        deadline: float | None = None,
    ) -> Any:
        deadline = deadline if deadline is not None else monotonic() + self._request_budget_seconds
        curl_timeout = self._attempt_timeout(deadline, TMDB_CURL_ATTEMPT_SECONDS)
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
            str(max(1, int(curl_timeout))),
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
                timeout=min(curl_timeout + 1.0, self._remaining_timeout(deadline)),
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            return json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as curl_error:
            try:
                return self._request_with_fragmented_tls(
                    method,
                    path,
                    headers=headers,
                    params=params,
                    deadline=deadline,
                )
            except Exception as fragmented_tls_error:
                if self._remaining_timeout(deadline, raise_if_expired=False) <= 0:
                    raise fragmented_tls_error from curl_error
                try:
                    return self._request_with_urllib(
                        method,
                        path,
                        headers=headers,
                        params=params,
                        deadline=deadline,
                    )
                except Exception:
                    raise fragmented_tls_error from curl_error

    def _request_with_fragmented_tls(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        deadline: float | None = None,
    ) -> Any:
        query = urlencode(params)
        target = f"/3{path}" if not query else f"/3{path}?{query}"
        deadline = deadline if deadline is not None else monotonic() + self._request_budget_seconds
        timeout = self._remaining_timeout(deadline)
        status, reason, response_headers, body = request_with_fragmented_tls(
            "api.themoviedb.org",
            target,
            method=method,
            headers=headers,
            timeout=timeout,
        )
        if status >= 400:
            raise HTTPError(f"{TMDB_API_URL}{target}", status, reason, response_headers, None)
        return json.loads(body.decode("utf-8"))

    def _request_with_urllib(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        deadline: float | None = None,
    ) -> Any:
        query = urlencode(params)
        url = f"{TMDB_API_URL}{path}"
        if query:
            url = f"{url}?{query}"
        request = UrlRequest(url, headers=headers, method=method.upper())
        deadline = deadline if deadline is not None else monotonic() + self._request_budget_seconds
        timeout = self._attempt_timeout(deadline, TMDB_URLLIB_ATTEMPT_SECONDS)
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _remaining_timeout(deadline: float, *, raise_if_expired: bool = True) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0 and raise_if_expired:
            raise TimeoutError("TMDb request budget exhausted")
        return max(0.0, remaining)

    def _attempt_timeout(self, deadline: float, maximum: float) -> float:
        return max(0.1, min(float(maximum), self._remaining_timeout(deadline)))

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
