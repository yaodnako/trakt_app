from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
)
from trakt_tracker.application.metadata_refresh_policy import (
    ARTWORK_EMPTY_REFRESH_SECONDS,
    ARTWORK_RETRYABLE_FAILURE_BACKOFF_SECONDS,
    ASSET_KIND_POSTER,
    ASSET_KIND_STILL,
    ASSET_KIND_TITLE_RATINGS,
    RATINGS_EMPTY_REFRESH_SECONDS,
    RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS,
    TITLE_RATINGS_READY_REFRESH_SECONDS,
    TRIGGER_MANUAL_REPAIR,
    TRIGGER_SYNC_EVENT,
    TRIGGER_VIEWPORT,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
    metadata_refresh_due,
)


class MetadataRefreshPolicyTests(unittest.TestCase):
    def test_title_ratings_ready_due_after_ttl(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_TITLE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=TITLE_RATINGS_READY_REFRESH_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "ready_ttl_elapsed")

    def test_title_ratings_checked_no_data_rechecks_after_hours(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_TITLE_RATINGS,
            status=ENRICH_STATUS_CHECKED_NO_DATA,
            last_checked_at=now - timedelta(seconds=RATINGS_EMPTY_REFRESH_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_VIEWPORT,
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "checked_no_data_ttl_elapsed")

    def test_artwork_ready_is_not_refreshed_on_sync_event(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_POSTER,
            status=ENRICH_STATUS_READY,
            last_checked_at=now,
            has_value=True,
            trigger=TRIGGER_SYNC_EVENT,
            now=now,
        )
        self.assertFalse(due.should_refresh)
        self.assertEqual(due.reason, "ready_artwork_not_rechecked")

    def test_artwork_checked_no_data_rechecks_after_long_ttl(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_STILL,
            status=ENRICH_STATUS_CHECKED_NO_DATA,
            last_checked_at=now - timedelta(seconds=ARTWORK_EMPTY_REFRESH_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_VIEWPORT,
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "checked_no_data_ttl_elapsed")

    def test_retryable_failure_respects_backoff_by_asset_kind(self) -> None:
        now = datetime.now(tz=UTC)
        ratings_due = metadata_refresh_due(
            ASSET_KIND_TITLE_RATINGS,
            status=ENRICH_STATUS_RETRYABLE_FAILURE,
            last_checked_at=now - timedelta(seconds=RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_VIEWPORT,
            now=now,
        )
        artwork_due = metadata_refresh_due(
            ASSET_KIND_POSTER,
            status=ENRICH_STATUS_RETRYABLE_FAILURE,
            last_checked_at=now - timedelta(seconds=ARTWORK_RETRYABLE_FAILURE_BACKOFF_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_SYNC_EVENT,
            now=now,
        )
        self.assertTrue(ratings_due.should_refresh)
        self.assertTrue(artwork_due.should_refresh)

    def test_manual_repair_overrides_ready_artwork(self) -> None:
        due = metadata_refresh_due(
            ASSET_KIND_POSTER,
            status=ENRICH_STATUS_READY,
            last_checked_at=datetime.now(tz=UTC),
            has_value=True,
            trigger=TRIGGER_MANUAL_REPAIR,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "manual_repair_override")


if __name__ == "__main__":
    unittest.main()
