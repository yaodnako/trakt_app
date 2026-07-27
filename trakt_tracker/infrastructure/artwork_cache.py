from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from typing import TypedDict
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener, urlopen

import httpx

from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.infrastructure.fragmented_https import request_with_fragmented_tls


_warm_lock = Lock()
_warm_running: set[str] = set()
_image_fetch_lock = Lock()
_image_fetch_limit = BoundedSemaphore(4)
_no_proxy_opener = build_opener(ProxyHandler({}))
_background_warm_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="artwork-warm")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
TRUSTED_IMAGE_HOSTS = {"media.trakt.tv", "walter.trakt.tv", "images.trakt.tv"}


class WarmImageResult(TypedDict):
    selected: int
    warmed: int
    failed: int
    skipped: int
    warmed_urls: list[str]
    failed_urls: list[str]


class _ImageFetchFlight:
    def __init__(self) -> None:
        self.event = Event()
        self.result: tuple[bytes, str] | None = None
        self.error: Exception | None = None


_image_fetch_flights: dict[str, _ImageFetchFlight] = {}


def is_trusted_image_url(value: str) -> bool:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if host == "image.tmdb.org":
        return parsed.path.startswith("/t/p/")
    return host in TRUSTED_IMAGE_HOSTS and parsed.path.startswith("/images/")


def _valid_image_payload(payload: bytes, content_type: str | None) -> bool:
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        return False
    media_type = str(content_type or "").split(";", 1)[0].strip().casefold()
    if media_type == "image/svg+xml" or (media_type and not media_type.startswith("image/")):
        return False
    return payload.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")) or (
        payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    ) or (len(payload) >= 12 and payload[4:12] in {b"ftypavif", b"ftypavis"})


def image_cache_suffix(url: str, content_type: str | None = None) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type:
        guessed = mimetypes.guess_extension(media_type)
        if guessed:
            return guessed
    guessed_from_url, _ = mimetypes.guess_type(url)
    if guessed_from_url:
        guessed = mimetypes.guess_extension(guessed_from_url)
        if guessed:
            return guessed
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix
    return ".img"


def has_cached_image(cache: BinaryCache, url: str) -> bool:
    if not url:
        return False
    contains = getattr(cache, "contains", None)
    if callable(contains):
        return bool(contains(url))
    return cache.get_any_bytes(url) is not None


def fetch_and_cache_image(cache: BinaryCache, target_url: str, timeout: float) -> tuple[bytes, str] | None:
    if not is_trusted_image_url(target_url):
        return None
    with _image_fetch_lock:
        flight = _image_fetch_flights.get(target_url)
        is_leader = flight is None
        if flight is None:
            flight = _ImageFetchFlight()
            _image_fetch_flights[target_url] = flight
    if not is_leader:
        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        return flight.result
    try:
        with _image_fetch_limit:
            flight.result = _fetch_and_cache_image_uncached(cache, target_url, timeout)
        return flight.result
    except Exception as exc:
        flight.error = exc
        raise
    finally:
        with _image_fetch_lock:
            if _image_fetch_flights.get(target_url) is flight:
                _image_fetch_flights.pop(target_url, None)
            flight.event.set()


def _fetch_and_cache_image_uncached(cache: BinaryCache, target_url: str, timeout: float) -> tuple[bytes, str] | None:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    candidates = _candidate_image_urls(target_url)
    if _is_tmdb_image_url(target_url):
        for fetch_url in candidates:
            fetched_by_fragmented_tls = _fetch_image_with_fragmented_tls(fetch_url, timeout, headers)
            if fetched_by_fragmented_tls is not None:
                fetched, content_type = fetched_by_fragmented_tls
                if _valid_image_payload(fetched, content_type):
                    cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
                    return fetched, content_type
            fetched_by_curl = _fetch_image_with_curl(fetch_url, timeout)
            if fetched_by_curl is not None:
                fetched, content_type = fetched_by_curl
                if _valid_image_payload(fetched, content_type):
                    cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
                    return fetched, content_type
        return None
    for fetch_url in candidates:
        fetched_by_curl = _fetch_image_with_curl(fetch_url, timeout)
        if fetched_by_curl is not None:
            fetched, content_type = fetched_by_curl
            if _valid_image_payload(fetched, content_type):
                cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
                return fetched, content_type
        fetched_by_helper = _fetch_image_with_python_helper(fetch_url, timeout)
        if fetched_by_helper is not None:
            fetched, content_type = fetched_by_helper
            if _valid_image_payload(fetched, content_type):
                cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
                return fetched, content_type
        fetched_direct = _fetch_image_with_httpx(fetch_url, timeout, headers)
        if fetched_direct is not None:
            fetched, content_type = fetched_direct
            if _valid_image_payload(fetched, content_type):
                cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
                return fetched, content_type
    return None


def _fetch_image_with_httpx(target_url: str, timeout: float, headers: dict[str, str]) -> tuple[bytes, str] | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, trust_env=True) as client:
            response = client.get(target_url)
            response.raise_for_status()
            fetched = response.content
            content_type = response.headers.get("Content-Type", "")
        if fetched:
            return fetched, content_type
    except Exception:
        pass
    return None


def _fetch_image_with_urlopen(target_url: str, timeout: float, headers: dict[str, str]) -> tuple[bytes, str] | None:
    for fetch in (urlopen, _no_proxy_opener.open):
        try:
            upstream_request = UrlRequest(target_url, headers=headers)
            with fetch(upstream_request, timeout=timeout) as upstream_response:
                fetched = upstream_response.read(MAX_IMAGE_BYTES + 1)
                content_type = upstream_response.headers.get("Content-Type", "")
            if not fetched:
                continue
            return fetched, content_type
        except Exception:
            continue
    return None


def _fetch_image_with_fragmented_tls(
    target_url: str,
    timeout: float,
    headers: dict[str, str],
) -> tuple[bytes, str] | None:
    current_url = target_url
    for _ in range(4):
        if not is_trusted_image_url(current_url):
            return None
        parsed = urlsplit(current_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        try:
            status, _, response_headers, payload = request_with_fragmented_tls(
                parsed.hostname,
                target,
                headers=headers,
                timeout=timeout,
            )
        except Exception:
            return None
        if status in {301, 302, 303, 307, 308}:
            location = str(response_headers.get("Location", "") or "")
            if not location:
                return None
            current_url = urljoin(current_url, location)
            continue
        if status >= 400 or not payload:
            return None
        if len(payload) > MAX_IMAGE_BYTES:
            return None
        return payload, str(response_headers.get("Content-Type", "") or "")
    return None


def _candidate_image_urls(target_url: str) -> list[str]:
    match = re.match(r"^(https://image\.tmdb\.org/t/p/)([^/]+)(/[^?#]+)(.*)$", target_url)
    if not match:
        return [target_url]
    prefix, current_size, path, suffix = match.groups()
    urls = []
    for size in (current_size, "w500", "w342", "w780", "original"):
        urls.append(f"{prefix}{size}{path}{suffix}")
    return list(dict.fromkeys(urls))


def tmdb_episode_preview_url(target_url: str) -> str:
    return re.sub(
        r"^(https://image\.tmdb\.org/t/p/)[^/]+(/[^?#]+(?:[?#].*)?)$",
        r"\1w342\2",
        target_url,
    )


def _is_tmdb_image_url(target_url: str) -> bool:
    return bool(re.match(r"^https://image\.tmdb\.org/t/p/", target_url))


def _fetch_image_with_curl(target_url: str, timeout: float) -> tuple[bytes, str] | None:
    fd, output_path = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    try:
        attempt_timeout = max(1, int(timeout or 1))
        retry_timeout = max(attempt_timeout + 5, attempt_timeout * 2)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(
            [
                "curl.exe",
                "--silent",
                "--show-error",
                "--ipv4",
                "--doh-url",
                "https://cloudflare-dns.com/dns-query",
                "--connect-timeout",
                "3",
                "--max-filesize",
                str(MAX_IMAGE_BYTES),
                "--max-time",
                str(attempt_timeout),
                "--retry",
                "2",
                "--retry-all-errors",
                "--retry-delay",
                "0",
                "--retry-max-time",
                str(retry_timeout),
                "--continue-at",
                "-",
                "-A",
                "Mozilla/5.0",
                "-o",
                output_path,
                "-w",
                "%{content_type}",
                target_url,
            ],
            capture_output=True,
            text=True,
            timeout=retry_timeout + 5,
            startupinfo=startupinfo,
            creationflags=creationflags,
            check=False,
        )
        if completed.returncode != 0:
            return None
        payload = Path(output_path).read_bytes()
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            return None
        return payload, completed.stdout.strip()
    except Exception:
        return None
    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError:
            pass


def warm_image_cache_in_background(cache: BinaryCache, target_url: str, *, timeout: float) -> None:
    target_url = target_url.strip()
    if not is_trusted_image_url(target_url):
        return
    with _warm_lock:
        if target_url in _warm_running:
            return
        _warm_running.add(target_url)

    def runner() -> None:
        try:
            fetch_and_cache_image(cache, target_url, timeout)
        except Exception:
            pass
        finally:
            with _warm_lock:
                _warm_running.discard(target_url)

    _background_warm_executor.submit(runner)


def warm_image_urls(
    cache: BinaryCache,
    urls,
    *,
    timeout: float = 8,
    max_workers: int = 4,
    skip_cached: bool = True,
) -> WarmImageResult:
    unique_urls = [str(url or "").strip() for url in dict.fromkeys(urls) if str(url or "").strip()]
    if skip_cached:
        unique_urls = [url for url in unique_urls if not has_cached_image(cache, url)]
    result: WarmImageResult = {
        "selected": len(unique_urls),
        "warmed": 0,
        "failed": 0,
        "skipped": 0,
        "warmed_urls": [],
        "failed_urls": [],
    }
    if not unique_urls:
        return result
    workers = max(1, int(max_workers or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_and_cache_image, cache, url, timeout): url
            for url in unique_urls
        }
        for future in as_completed(futures):
            try:
                if future.result() is not None:
                    result["warmed"] += 1
                    result["warmed_urls"].append(futures[future])
                else:
                    result["failed"] += 1
                    result["failed_urls"].append(futures[future])
            except Exception:
                result["failed"] += 1
                result["failed_urls"].append(futures[future])
    return result


def _fetch_image_with_python_helper(target_url: str, timeout: float) -> tuple[bytes, str] | None:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if not executable.exists():
        return None
    fd, output_path = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    try:
        code = (
            "import pathlib, sys, httpx\n"
            "url, output_path, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
            "headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'}\n"
            "with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, trust_env=True) as client:\n"
            "    response = client.get(url)\n"
            "    response.raise_for_status()\n"
            f"    payload = response.content\n    if len(payload) > {MAX_IMAGE_BYTES}: raise ValueError('image is too large')\n"
            "    pathlib.Path(output_path).write_bytes(payload)\n"
            "    print(response.headers.get('Content-Type', ''))\n"
        )
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(
            [str(executable), "-c", code, target_url, output_path, str(timeout)],
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout) + 5),
            startupinfo=startupinfo,
            creationflags=creationflags,
            check=False,
        )
        if completed.returncode != 0:
            return None
        payload = Path(output_path).read_bytes()
        if not payload:
            return None
        content_type = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        return payload, content_type
    except Exception:
        return None
    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError:
            pass
