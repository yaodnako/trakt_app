from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import HistoryRepository, NotificationRepository, ProgressRepository


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmpdir.name) / "test.sqlite3")
        self.db.create_schema()

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def test_history_repository_deduplicates_by_trakt_history_id(self) -> None:
        repo = HistoryRepository()
        watched_at = datetime.now(tz=UTC)
        with self.db.session() as session:
            repo.add_event(
                session,
                trakt_history_id=10,
                title_trakt_id=1,
                title="Example",
                title_type="movie",
                action="watched",
                watched_at=watched_at,
            )
            repo.add_event(
                session,
                trakt_history_id=10,
                title_trakt_id=1,
                title="Example",
                title_type="movie",
                action="watched",
                watched_at=watched_at,
            )
            rows = repo.list_recent(session)
        self.assertEqual(len(rows), 1)

    def test_progress_repository_roundtrip(self) -> None:
        repo = ProgressRepository()
        progress = ProgressSnapshot(
            trakt_id=42,
            title="Sample Show",
            completed=8,
            aired=10,
            percent_completed=80.0,
            next_episode=EpisodeSummary(trakt_id=100, season=1, number=9, title="Next"),
            last_episode=EpisodeSummary(trakt_id=99, season=1, number=8, title="Last"),
        )
        with self.db.session() as session:
            repo.upsert_progress(session, progress)
            rows = repo.list_in_progress(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Sample Show")
        self.assertEqual(rows[0].next_episode.number, 9)

    def test_notification_repository_prevents_duplicates(self) -> None:
        repo = NotificationRepository()
        with self.db.session() as session:
            repo.mark_sent(
                session,
                show_trakt_id=7,
                show_title="Tracked Show",
                episode_trakt_id=70,
                season=2,
                episode=1,
                message="S02E01",
            )
            first_log = repo.get_log(session, 7, 70)
            repo.mark_sent(
                session,
                show_trakt_id=7,
                show_title="Tracked Show",
                episode_trakt_id=70,
                season=2,
                episode=1,
                message="S02E01",
            )
            repo.mark_seen(
                session,
                show_trakt_id=7,
                show_title="Tracked Show",
                episode_trakt_id=70,
                season=2,
                episode=1,
                message="S02E01",
            )
            log = repo.get_log(session, 7, 70)
            unseen_ids = repo.unseen_episode_ids(session)
        self.assertIsNotNone(first_log)
        self.assertIsNotNone(log)
        self.assertEqual(log.notify_count, 2)
        self.assertIsNotNone(log.seen_at)
        self.assertNotIn(70, unseen_ids)


if __name__ == "__main__":
    unittest.main()
