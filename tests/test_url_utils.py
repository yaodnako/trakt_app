from __future__ import annotations

import unittest

from trakt_tracker.infrastructure.url_utils import normalize_external_url


class UrlUtilsTests(unittest.TestCase):
    def test_preserves_absolute_urls(self) -> None:
        self.assertEqual(normalize_external_url("https://example.com/a.jpg"), "https://example.com/a.jpg")

    def test_adds_https_to_scheme_less_host(self) -> None:
        self.assertEqual(
            normalize_external_url("media.trakt.tv/images/movies/poster.webp"),
            "https://media.trakt.tv/images/movies/poster.webp",
        )

    def test_adds_https_to_protocol_relative_url(self) -> None:
        self.assertEqual(
            normalize_external_url("//media.trakt.tv/images/movies/poster.webp"),
            "https://media.trakt.tv/images/movies/poster.webp",
        )


if __name__ == "__main__":
    unittest.main()
