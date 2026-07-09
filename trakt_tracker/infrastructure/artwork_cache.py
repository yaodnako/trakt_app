from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock, Thread
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener, urlopen

import httpx

from trakt_tracker.infrastructure.cache import BinaryCache


_warm_lock = Lock()
_warm_running: set[str] = set()
_no_proxy_opener = build_opener(ProxyHandler({}))


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
    return bool(url and cache.get_any_bytes(url) is not None)


def fetch_and_cache_image(cache: BinaryCache, target_url: str, timeout: float) -> tuple[bytes, str] | None:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    candidates = _candidate_image_urls(target_url)
    if _is_tmdb_image_url(target_url):
        for fetch_url in candidates:
            fetched_by_curl = _fetch_image_with_curl(fetch_url, timeout)
            if fetched_by_curl is not None:
                fetched, content_type = fetched_by_curl
                cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
                return fetched, content_type
        return None
    for fetch_url in candidates:
        fetched_by_curl = _fetch_image_with_curl(fetch_url, timeout)
        if fetched_by_curl is not None:
            fetched, content_type = fetched_by_curl
            cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
            return fetched, content_type
        fetched_by_helper = _fetch_image_with_python_helper(fetch_url, timeout)
        if fetched_by_helper is not None:
            fetched, content_type = fetched_by_helper
            cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
            return fetched, content_type
        fetched_direct = _fetch_image_with_httpx(fetch_url, timeout, headers)
        if fetched_direct is not None:
            fetched, content_type = fetched_direct
            cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
            return fetched, content_type
        fetched_urlopen = _fetch_image_with_urlopen(fetch_url, timeout, headers)
        if fetched_urlopen is not None:
            fetched, content_type = fetched_urlopen
            cache.set_bytes(target_url, fetched, suffix=image_cache_suffix(fetch_url, content_type))
            return fetched, content_type
    return None


def _fetch_image_with_httpx(target_url: str, timeout: float, headers: dict[str, str]) -> tuple[bytes, str] | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers, trust_env=True) as client:
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
                fetched = upstream_response.read()
                content_type = upstream_response.headers.get("Content-Type", "")
            if not fetched:
                continue
            return fetched, content_type
        except Exception:
            continue
    return None


def _candidate_image_urls(target_url: str) -> list[str]:
    match = re.match(r"^(https://image\.tmdb\.org/t/p/)([^/]+)(/[^?#]+)(.*)$", target_url)
    if not match:
        return [target_url]
    prefix, current_size, path, suffix = match.groups()
    urls = []
    for size in ("w780", "w500", "w342", "original", current_size):
        urls.append(f"{prefix}{size}{path}{suffix}")
    return list(dict.fromkeys(urls))


def _is_tmdb_image_url(target_url: str) -> bool:
    return bool(re.match(r"^https://image\.tmdb\.org/t/p/", target_url))


def _fetch_image_with_curl(target_url: str, timeout: float) -> tuple[bytes, str] | None:
    fd, output_path = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    try:
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(
            [
                "curl.exe",
                "-L",
                "--silent",
                "--show-error",
                "--ipv4",
                "--doh-url",
                "https://cloudflare-dns.com/dns-query",
                "--connect-timeout",
                "3",
                "--max-time",
                str(max(1, int(timeout or 1))),
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
            timeout=max(5, int(timeout or 1) + 5),
            startupinfo=startupinfo,
            creationflags=creationflags,
            check=False,
        )
        if completed.returncode != 0:
            return None
        payload = Path(output_path).read_bytes()
        if not payload:
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
    if not target_url:
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

    Thread(target=runner, daemon=True).start()


def warm_image_urls(
    cache: BinaryCache,
    urls,
    *,
    timeout: float = 8,
    max_workers: int = 4,
    skip_cached: bool = True,
) -> dict[str, int]:
    unique_urls = [str(url or "").strip() for url in dict.fromkeys(urls) if str(url or "").strip()]
    if skip_cached:
        unique_urls = [url for url in unique_urls if not has_cached_image(cache, url)]
    result = {"selected": len(unique_urls), "warmed": 0, "failed": 0, "skipped": 0}
    warmed_urls: list[str] = []
    failed_urls: list[str] = []
    if not unique_urls:
        result["warmed_urls"] = warmed_urls
        result["failed_urls"] = failed_urls
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
                    warmed_urls.append(futures[future])
                else:
                    result["failed"] += 1
                    failed_urls.append(futures[future])
            except Exception:
                result["failed"] += 1
                failed_urls.append(futures[future])
    result["warmed_urls"] = warmed_urls
    result["failed_urls"] = failed_urls
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
            "with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers, trust_env=True) as client:\n"
            "    response = client.get(url)\n"
            "    response.raise_for_status()\n"
            "    pathlib.Path(output_path).write_bytes(response.content)\n"
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
