from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx

from trakt_tracker.infrastructure.kinopoisk import (
    KinopoiskClient,
    build_film_url,
    extract_first_film_id,
    normalize_cache_key,
)


class KinopoiskUtilsTests(unittest.TestCase):
    def test_normalize_cache_key(self) -> None:
        self.assertEqual(normalize_cache_key("  The   Expanse "), "the expanse")

    def test_extracts_first_valid_film_id(self) -> None:
        payload = {"films": [{"filmId": None}, {"filmId": "321"}]}
        self.assertEqual(extract_first_film_id(payload), 321)

    def test_build_urls(self) -> None:
        self.assertEqual(build_film_url(123, "net"), "https://www.kinopoisk.net/film/123/")


class KinopoiskClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = TemporaryDirectory()
        self._app_dir_patch = patch(
            "trakt_tracker.infrastructure.cache.get_app_data_dir",
            return_value=Path(self._tmp_dir.name),
        )
        self._app_dir_patch.start()

    def tearDown(self) -> None:
        self._app_dir_patch.stop()
        self._tmp_dir.cleanup()

    def test_uses_cache_before_api(self) -> None:
        client = KinopoiskClient(api_key="secret")
        client._cache.set_json("the expanse", {"filmId": 123})
        client._client = Mock()
        url = client.resolve_title_url("The Expanse", domain="net")
        self.assertEqual(url, "https://www.kinopoisk.net/film/123/")
        client._client.get.assert_not_called()

    def test_returns_none_when_api_has_no_film_id(self) -> None:
        client = KinopoiskClient(api_key="secret")
        response = Mock()
        response.json.return_value = {"films": [{"nameRu": "Test"}]}
        response.raise_for_status.return_value = None
        client._client = Mock()
        client._client.get.return_value = response
        url = client.resolve_title_url("Test title", domain="net")
        self.assertIsNone(url)

    def test_returns_none_when_api_errors(self) -> None:
        client = KinopoiskClient(api_key="secret")
        request = httpx.Request("GET", "https://kinopoiskapiunofficial.tech")
        response = httpx.Response(500, request=request)
        client._client = Mock()
        client._client.get.side_effect = httpx.HTTPStatusError("boom", request=request, response=response)
        url = client.resolve_title_url("Severance", domain="net")
        self.assertIsNone(url)

    def test_skips_api_when_key_missing(self) -> None:
        client = KinopoiskClient(api_key="")
        client._client = Mock()
        url = client.resolve_title_url("Dark", domain="net")
        self.assertIsNone(url)
        client._client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
