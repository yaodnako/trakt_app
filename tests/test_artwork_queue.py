from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from trakt_tracker.infrastructure.artwork_queue import ArtworkQueue


class ArtworkQueueTests(unittest.TestCase):
    @patch("trakt_tracker.infrastructure.artwork_queue.fetch_and_cache_image", return_value=b"image")
    @patch("trakt_tracker.infrastructure.artwork_queue.has_cached_image", return_value=False)
    @patch("trakt_tracker.infrastructure.artwork_queue.is_trusted_image_url", return_value=True)
    def test_pauses_and_deduplicates_urls_before_dispatch(self, _trusted, _cached, fetch) -> None:
        queue = ArtworkQueue(object(), max_workers=1)
        try:
            queue.pause()
            self.assertTrue(queue.submit("https://image.tmdb.org/t/p/w342/a.jpg", priority=3))
            self.assertTrue(queue.submit("https://image.tmdb.org/t/p/w342/a.jpg", priority=1))
            time.sleep(0.05)
            fetch.assert_not_called()
            queue.resume()
            self._wait_until(lambda: fetch.call_count == 1)
            self.assertEqual(queue.status_snapshot()["pending"], 0)
        finally:
            self.assertTrue(queue.close())

    @patch("trakt_tracker.infrastructure.artwork_queue.fetch_and_cache_image", return_value=b"image")
    @patch("trakt_tracker.infrastructure.artwork_queue.has_cached_image", return_value=False)
    @patch("trakt_tracker.infrastructure.artwork_queue.is_trusted_image_url", return_value=True)
    def test_tmdb_proxy_is_resolved_when_background_work_runs(self, _trusted, _cached, fetch) -> None:
        queue = ArtworkQueue(
            object(),
            max_workers=1,
            proxy_url_provider=lambda: "socks5h://127.0.0.1:10808",
        )
        try:
            self.assertTrue(queue.submit("https://image.tmdb.org/t/p/w342/a.jpg"))
            self._wait_until(lambda: fetch.call_count == 1)
            fetch.assert_called_once_with(
                queue._cache,
                "https://image.tmdb.org/t/p/w342/a.jpg",
                queue._timeout,
                proxy_url="socks5h://127.0.0.1:10808",
            )
        finally:
            self.assertTrue(queue.close())

    @staticmethod
    def _wait_until(predicate, *, timeout: float = 1.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            threading.Event().wait(0.01)
        raise AssertionError("Timed out waiting for image work")
