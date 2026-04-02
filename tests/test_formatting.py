from __future__ import annotations

import unittest

from trakt_tracker.formatting import format_compact_votes, format_progress_percent, format_rating_with_votes


class FormattingTests(unittest.TestCase):
    def test_format_compact_votes_keeps_plain_numbers_under_thousand(self) -> None:
        self.assertEqual(format_compact_votes(579), "579")

    def test_format_compact_votes_rounds_thousands_without_fraction(self) -> None:
        self.assertEqual(format_compact_votes(5780), "5.8k")
        self.assertEqual(format_compact_votes(1470), "1.5k")

    def test_format_compact_votes_keeps_two_decimals_for_millions(self) -> None:
        self.assertEqual(format_compact_votes(1830000), "1.83m")
        self.assertEqual(format_compact_votes(1200000), "1.2m")

    def test_format_progress_percent_removes_fraction(self) -> None:
        self.assertEqual(format_progress_percent(75.0), "75%")
        self.assertEqual(format_progress_percent(66.7), "67%")

    def test_format_rating_with_votes_uses_compact_vote_format(self) -> None:
        self.assertEqual(format_rating_with_votes(8.4, 5780), "8.4 (5.8k)")
        self.assertEqual(format_rating_with_votes(7.5, 1830000), "7.5 (1.83m)")


if __name__ == "__main__":
    unittest.main()
