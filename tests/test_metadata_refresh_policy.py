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
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_POSTER,
    ASSET_KIND_STILL,
    ASSET_KIND_TITLE_RATINGS,
    EPISODE_RATINGS_BACKGROUND_BUCKET_10_TO_60_SECONDS,
    EPISODE_RATINGS_BACKGROUND_BUCKET_180_TO_720_SECONDS,
    EPISODE_RATINGS_BACKGROUND_BUCKET_60_TO_180_SECONDS,
    EPISODE_RATINGS_BACKGROUND_BUCKET_720_PLUS_SECONDS,
    EPISODE_RATINGS_FOREGROUND_WINDOW_SECONDS,
    RATINGS_EMPTY_REFRESH_SECONDS,
    RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS,
    STILL_RECENT_EMPTY_REFRESH_SECONDS,
    STILL_RECENT_RELEASE_WINDOW_SECONDS,
    STILL_VISIBLE_EMPTY_REFRESH_SECONDS,
    TRIGGER_BACKGROUND_SWEEP,
    TITLE_RATINGS_READY_REFRESH_SECONDS,
    TRIGGER_MANUAL_REPAIR,
    TRIGGER_PAGE_CONTEXT,
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

    def test_recent_episode_trakt_ready_refreshes_in_foreground_after_five_minutes(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=TITLE_RATINGS_READY_REFRESH_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
            first_aired=now - timedelta(days=5),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "ready_ttl_elapsed")

    def test_old_episode_trakt_ready_skips_foreground_refresh(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=TITLE_RATINGS_READY_REFRESH_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
            first_aired=now - timedelta(days=30),
            now=now,
        )
        self.assertFalse(due.should_refresh)
        self.assertEqual(due.reason, "background_refresh_window")

    def test_episode_trakt_background_10_to_60_day_bucket(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_10_TO_60_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_BACKGROUND_SWEEP,
            first_aired=now - timedelta(days=30),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "background_10_60_ttl_elapsed")

    def test_episode_trakt_background_60_to_180_day_bucket(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_60_TO_180_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_BACKGROUND_SWEEP,
            first_aired=now - timedelta(days=120),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "background_60_180_ttl_elapsed")

    def test_episode_trakt_background_180_to_720_day_bucket(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_180_TO_720_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_BACKGROUND_SWEEP,
            first_aired=now - timedelta(days=300),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "background_180_720_ttl_elapsed")

    def test_episode_trakt_background_720_plus_day_bucket(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_720_PLUS_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_BACKGROUND_SWEEP,
            first_aired=now - timedelta(days=900),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "background_720_plus_ttl_elapsed")

    def test_episode_trakt_background_skips_foreground_window(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_EPISODE_RATINGS,
            status=ENRICH_STATUS_READY,
            last_checked_at=now - timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_10_TO_60_SECONDS + 1),
            has_value=True,
            trigger=TRIGGER_BACKGROUND_SWEEP,
            first_aired=now - timedelta(seconds=EPISODE_RATINGS_FOREGROUND_WINDOW_SECONDS - 60),
            now=now,
        )
        self.assertFalse(due.should_refresh)
        self.assertEqual(due.reason, "background_foreground_window_active")

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

    def test_recent_released_still_checked_no_data_rechecks_quickly_for_viewport(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_STILL,
            status=ENRICH_STATUS_CHECKED_NO_DATA,
            last_checked_at=now - timedelta(seconds=STILL_VISIBLE_EMPTY_REFRESH_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_VIEWPORT,
            first_aired=now - timedelta(days=1),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "checked_no_data_recent_visible_ttl_elapsed")

    def test_recent_released_still_checked_no_data_rechecks_hourly_for_page_context(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_STILL,
            status=ENRICH_STATUS_CHECKED_NO_DATA,
            last_checked_at=now - timedelta(seconds=STILL_RECENT_EMPTY_REFRESH_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_PAGE_CONTEXT,
            first_aired=now - timedelta(days=1),
            now=now,
        )
        self.assertTrue(due.should_refresh)
        self.assertEqual(due.reason, "checked_no_data_recent_ttl_elapsed")

    def test_unreleased_still_checked_no_data_keeps_long_ttl(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_STILL,
            status=ENRICH_STATUS_CHECKED_NO_DATA,
            last_checked_at=now - timedelta(seconds=STILL_VISIBLE_EMPTY_REFRESH_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_VIEWPORT,
            first_aired=now + timedelta(days=1),
            now=now,
        )
        self.assertFalse(due.should_refresh)
        self.assertEqual(due.reason, "checked_no_data_ttl_active")

    def test_old_released_still_checked_no_data_keeps_long_ttl(self) -> None:
        now = datetime.now(tz=UTC)
        due = metadata_refresh_due(
            ASSET_KIND_STILL,
            status=ENRICH_STATUS_CHECKED_NO_DATA,
            last_checked_at=now - timedelta(seconds=STILL_RECENT_EMPTY_REFRESH_SECONDS + 1),
            has_value=False,
            trigger=TRIGGER_PAGE_CONTEXT,
            first_aired=now - timedelta(seconds=STILL_RECENT_RELEASE_WINDOW_SECONDS + 1),
            now=now,
        )
        self.assertFalse(due.should_refresh)
        self.assertEqual(due.reason, "checked_no_data_ttl_active")

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
