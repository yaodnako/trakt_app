from __future__ import annotations

import unittest

from trakt_tracker.domain import TitleSummary
from trakt_tracker.web.viewmodels import (
    normalize_search_sort_mode,
    normalize_title_type,
    saved_search_matches,
    sort_search_results,
)


class WebViewModelTests(unittest.TestCase):
    def test_normalize_title_type_accepts_known_values(self) -> None:
        self.assertEqual(normalize_title_type("movie"), "movie")
        self.assertEqual(normalize_title_type("SHOW"), "show")
        self.assertIsNone(normalize_title_type("all"))

    def test_normalize_search_sort_mode_uses_fallback(self) -> None:
        self.assertEqual(normalize_search_sort_mode("", "Alphabetical"), "Alphabetical")
        self.assertEqual(normalize_search_sort_mode("unknown", "bad"), "IMDb votes")

    def test_saved_search_matches_respects_title_type(self) -> None:
        state = {"query": "Dune", "title_type": "all", "results": [object()]}
        self.assertTrue(saved_search_matches(state, "Dune", None))
        self.assertFalse(saved_search_matches(state, "Dune", "movie"))

    def test_sort_search_results_by_imdb_votes_descending(self) -> None:
        results = [
            TitleSummary(trakt_id=1, title_type="movie", title="B", imdb_votes=100, imdb_rating=7.0),
            TitleSummary(trakt_id=2, title_type="movie", title="A", imdb_votes=500, imdb_rating=6.0),
            TitleSummary(trakt_id=3, title_type="movie", title="C", imdb_votes=500, imdb_rating=8.0),
        ]
        ordered = sort_search_results(results, "IMDb votes")
        self.assertEqual([item.trakt_id for item in ordered], [3, 2, 1])

    def test_sort_search_results_alphabetically(self) -> None:
        results = [
            TitleSummary(trakt_id=1, title_type="movie", title="Zulu", year=2024),
            TitleSummary(trakt_id=2, title_type="movie", title="alpha", year=2025),
        ]
        ordered = sort_search_results(results, "Alphabetical")
        self.assertEqual([item.trakt_id for item in ordered], [2, 1])


if __name__ == "__main__":
    unittest.main()
