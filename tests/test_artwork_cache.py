from __future__ import annotations

import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from threading import Event, Lock
from time import sleep
from unittest.mock import MagicMock, patch

from trakt_tracker.infrastructure.artwork_cache import (
    _candidate_image_urls,
    _fetch_and_cache_image_uncached,
    _fetch_image_with_curl,
    _fetch_image_with_fragmented_tls,
    fetch_and_cache_image,
    is_trusted_image_url,
    tmdb_episode_preview_url,
)


class ArtworkCacheTests(unittest.TestCase):
    def test_tmdb_candidates_try_requested_size_first(self) -> None:
        target = "https://image.tmdb.org/t/p/w342/example.jpg"

        self.assertEqual(_candidate_image_urls(target)[0], target)

    def test_fetch_rejects_untrusted_url_without_opening_a_connection(self) -> None:
        with patch("trakt_tracker.infrastructure.artwork_cache._fetch_and_cache_image_uncached") as fetch:
            result = fetch_and_cache_image(MagicMock(), "https://127.0.0.1/secret.jpg", 5)
        self.assertIsNone(result)
        fetch.assert_not_called()
        self.assertTrue(is_trusted_image_url("https://image.tmdb.org/t/p/w342/image.jpg"))
        self.assertTrue(is_trusted_image_url("https://media.trakt.tv/images/movies/000/001/posters/original.jpg"))
        self.assertFalse(is_trusted_image_url("https://image.tmdb.org/other/image.jpg"))

    def test_episode_preview_uses_card_sized_tmdb_image(self) -> None:
        self.assertEqual(
            tmdb_episode_preview_url("https://image.tmdb.org/t/p/w780/example.jpg"),
            "https://image.tmdb.org/t/p/w342/example.jpg",
        )
        self.assertEqual(
            tmdb_episode_preview_url("https://example.com/w780/example.jpg"),
            "https://example.com/w780/example.jpg",
        )

    def test_tmdb_image_fetch_uses_fragmented_tls_before_curl(self) -> None:
        cache = MagicMock()
        payload = b"\xff\xd8\xffimage"
        calls: list[str] = []

        def fragmented(*_args, **_kwargs):
            calls.append("fragmented")
            return payload, "image/jpeg"

        def curl(*_args, **_kwargs):
            calls.append("curl")
            return payload, "image/jpeg"

        with (
            patch(
                "trakt_tracker.infrastructure.artwork_cache._fetch_image_with_fragmented_tls",
                side_effect=fragmented,
            ),
            patch(
                "trakt_tracker.infrastructure.artwork_cache._fetch_image_with_curl",
                side_effect=curl,
            ),
        ):
            result = _fetch_and_cache_image_uncached(
                cache,
                "https://image.tmdb.org/t/p/w342/example.jpg",
                8,
            )

        self.assertEqual(result, (payload, "image/jpeg"))
        self.assertEqual(calls, ["fragmented"])

    def test_tmdb_image_fetch_keeps_curl_as_fragmented_tls_fallback(self) -> None:
        cache = MagicMock()
        payload = b"\xff\xd8\xffimage"
        calls: list[str] = []

        def fragmented(*_args, **_kwargs):
            calls.append("fragmented")
            return None

        def curl(*_args, **_kwargs):
            calls.append("curl")
            return payload, "image/jpeg"

        with (
            patch(
                "trakt_tracker.infrastructure.artwork_cache._fetch_image_with_fragmented_tls",
                side_effect=fragmented,
            ),
            patch(
                "trakt_tracker.infrastructure.artwork_cache._fetch_image_with_curl",
                side_effect=curl,
            ),
        ):
            result = _fetch_and_cache_image_uncached(
                cache,
                "https://image.tmdb.org/t/p/w342/example.jpg",
                8,
            )

        self.assertEqual(result, (payload, "image/jpeg"))
        self.assertEqual(calls, ["fragmented", "curl"])

    def test_concurrent_same_url_uses_one_shared_fetch(self) -> None:
        started = Event()
        release = Event()
        call_count = 0
        call_lock = Lock()
        shared_result = (b"image-bytes", "image/jpeg")

        def fetch(_cache, _url, _timeout):
            nonlocal call_count
            with call_lock:
                call_count += 1
            started.set()
            release.wait(timeout=2)
            return shared_result

        with patch("trakt_tracker.infrastructure.artwork_cache._fetch_and_cache_image_uncached", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(fetch_and_cache_image, MagicMock(), "https://image.tmdb.org/t/p/w342/image.jpg", 5)
                self.assertTrue(started.wait(timeout=1))
                second = executor.submit(fetch_and_cache_image, MagicMock(), "https://image.tmdb.org/t/p/w342/image.jpg", 5)
                sleep(0.05)
                self.assertEqual(call_count, 1)
                release.set()
                self.assertEqual(first.result(timeout=1), shared_result)
                self.assertEqual(second.result(timeout=1), shared_result)

    def test_distinct_image_fetches_are_limited_to_four(self) -> None:
        release = Event()
        four_started = Event()
        active = 0
        maximum_active = 0
        active_lock = Lock()

        def fetch(_cache, _url, _timeout):
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 4:
                    four_started.set()
            release.wait(timeout=2)
            with active_lock:
                active -= 1
            return b"image", "image/jpeg"

        with patch("trakt_tracker.infrastructure.artwork_cache._fetch_and_cache_image_uncached", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(fetch_and_cache_image, MagicMock(), f"https://image.tmdb.org/t/p/w342/image-{index}.jpg", 5)
                    for index in range(8)
                ]
                self.assertTrue(four_started.wait(timeout=1))
                sleep(0.05)
                self.assertEqual(maximum_active, 4)
                release.set()
                self.assertTrue(all(future.result(timeout=1) for future in futures))

    def test_failed_flight_does_not_block_next_attempt(self) -> None:
        with patch(
            "trakt_tracker.infrastructure.artwork_cache._fetch_and_cache_image_uncached",
            side_effect=RuntimeError("fetch failed"),
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                fetch_and_cache_image(MagicMock(), "https://image.tmdb.org/t/p/w342/failing.jpg", 5)
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                fetch_and_cache_image(MagicMock(), "https://image.tmdb.org/t/p/w342/failing.jpg", 5)

        self.assertEqual(fetch.call_count, 2)

    def test_curl_image_fetch_retries_and_resumes_partial_download(self) -> None:
        def run(command, **_kwargs):
            output_path = command[command.index("-o") + 1]
            with open(output_path, "wb") as handle:
                handle.write(b"complete-image")
            return subprocess.CompletedProcess(command, 0, stdout="image/jpeg", stderr="")

        with patch("trakt_tracker.infrastructure.artwork_cache.subprocess.run", side_effect=run) as execute:
            result = _fetch_image_with_curl("https://image.tmdb.org/t/p/w780/example.jpg", 8)

        self.assertEqual(result, (b"complete-image", "image/jpeg"))
        command = execute.call_args.args[0]
        self.assertEqual(command[command.index("--retry") + 1], "2")
        self.assertIn("--retry-all-errors", command)
        self.assertEqual(command[command.index("--continue-at") + 1], "-")
        self.assertEqual(execute.call_args.kwargs["timeout"], 21)

    def test_fragmented_tls_image_fetch_returns_payload_and_content_type(self) -> None:
        response_headers = Message()
        response_headers["Content-Type"] = "image/jpeg"

        with patch(
            "trakt_tracker.infrastructure.artwork_cache.request_with_fragmented_tls",
            return_value=(200, "OK", response_headers, b"image-bytes"),
        ):
            result = _fetch_image_with_fragmented_tls(
                "https://image.tmdb.org/t/p/w780/example.jpg",
                5,
                {"Accept": "image/*"},
            )

        self.assertEqual(result, (b"image-bytes", "image/jpeg"))

    def test_fragmented_tls_image_fetch_follows_https_redirect(self) -> None:
        redirect_headers = Message()
        redirect_headers["Location"] = "https://image.tmdb.org/t/p/w342/image.jpg"
        image_headers = Message()
        image_headers["Content-Type"] = "image/jpeg"

        with patch(
            "trakt_tracker.infrastructure.artwork_cache.request_with_fragmented_tls",
            side_effect=[
                (302, "Found", redirect_headers, b""),
                (200, "OK", image_headers, b"image-bytes"),
            ],
        ) as request:
            result = _fetch_image_with_fragmented_tls(
                "https://image.tmdb.org/t/p/w780/example.jpg",
                5,
                {"Accept": "image/*"},
            )

        self.assertEqual(result, (b"image-bytes", "image/jpeg"))
        self.assertEqual(request.call_count, 2)

    def test_fragmented_tls_image_fetch_rejects_redirect_to_untrusted_host(self) -> None:
        redirect_headers = Message()
        redirect_headers["Location"] = "https://127.0.0.1/secret.jpg"
        with patch(
            "trakt_tracker.infrastructure.artwork_cache.request_with_fragmented_tls",
            return_value=(302, "Found", redirect_headers, b""),
        ) as request:
            result = _fetch_image_with_fragmented_tls(
                "https://image.tmdb.org/t/p/w780/example.jpg",
                5,
                {"Accept": "image/*"},
            )
        self.assertIsNone(result)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
