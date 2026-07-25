from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from trakt_tracker.application.enrich_state import ENRICH_STATUS_CHECKED_NO_DATA, ENRICH_STATUS_READY
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot, TitleSummary
from trakt_tracker.persistence.database import LATEST_SCHEMA_VERSION, Database
from trakt_tracker.persistence.repositories import EpisodeRepository, HistoryRepository, NotificationRepository, ProgressRepository, TitleRepository


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

    def test_history_repository_returns_titles_with_any_watched_event(self) -> None:
        repo = HistoryRepository()
        watched_at = datetime.now(tz=UTC)
        with self.db.session() as session:
            repo.add_event(
                session,
                trakt_history_id=11,
                title_trakt_id=101,
                title="Example Show",
                title_type="show",
                action="watched",
                watched_at=watched_at,
                season=1,
                episode=1,
            )
            repo.add_event(
                session,
                trakt_history_id=12,
                title_trakt_id=202,
                title="Rated Only",
                title_type="movie",
                action="rated",
                watched_at=watched_at,
                rating=8,
            )
            keys = repo.watched_title_keys(session)
        self.assertEqual(keys, {("show", 101)})

    def test_history_reconciliation_removes_only_missing_trakt_watches_in_scanned_window(self) -> None:
        repo = HistoryRepository()
        now = datetime.now(tz=UTC)
        with self.db.session() as session:
            for history_id, watched_at, source, action in (
                (10, now, "trakt", "watched"),
                (11, now - timedelta(minutes=1), "trakt", "watched"),
                (12, now - timedelta(days=2), "trakt", "watched"),
                (None, now, "local", "watched"),
                (13, now, "trakt", "rated"),
            ):
                repo.add_event(
                    session,
                    trakt_history_id=history_id,
                    title_trakt_id=100 + int(history_id or 0),
                    title=f"Item {history_id}",
                    title_type="show",
                    action=action,
                    watched_at=watched_at,
                    season=1,
                    episode=int(history_id or 1),
                    source=source,
                )
            removed, removed_count = repo.delete_missing_trakt_watches(
                session,
                title_type="show",
                present_history_ids={10},
                watched_at_cutoff=now - timedelta(hours=1),
            )
            rows = repo.list_recent(session)

        self.assertEqual(removed, {("show", 111)})
        self.assertEqual(removed_count, 1)
        self.assertEqual({row.trakt_history_id for row in rows}, {10, 12, 13, None})

    def test_complete_history_reconciliation_is_type_scoped(self) -> None:
        repo = HistoryRepository()
        watched_at = datetime.now(tz=UTC)
        with self.db.session() as session:
            repo.add_event(
                session,
                trakt_history_id=20,
                title_trakt_id=1,
                title="Show",
                title_type="show",
                action="watched",
                watched_at=watched_at,
                season=1,
                episode=1,
                source="trakt",
            )
            repo.add_event(
                session,
                trakt_history_id=21,
                title_trakt_id=2,
                title="Movie",
                title_type="movie",
                action="watched",
                watched_at=watched_at,
                source="trakt",
            )
            _removed, removed_count = repo.delete_missing_trakt_watches(
                session,
                title_type="show",
                present_history_ids=set(),
            )
            rows = repo.list_recent(session)

        self.assertEqual([(row.title_type, row.trakt_history_id) for row in rows], [("movie", 21)])
        self.assertEqual(removed_count, 1)

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

    def test_progress_repository_hides_zero_completed_show(self) -> None:
        repo = ProgressRepository()
        with self.db.session() as session:
            repo.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=43,
                    title="Unwatched Show",
                    completed=0,
                    aired=12,
                    percent_completed=0.0,
                    next_episode=EpisodeSummary(trakt_id=101, season=1, number=1, title="Pilot"),
                ),
            )
            rows = repo.list_in_progress(session)
        self.assertEqual(rows, [])

    def test_progress_repository_displays_cached_title_when_progress_title_is_fallback(self) -> None:
        titles = TitleRepository()
        repo = ProgressRepository()
        with self.db.session() as session:
            titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=135985,
                    title_type="show",
                    title="That Time I Got Reincarnated as a Slime",
                ),
            )
            repo.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=135985,
                    title="Show 135985",
                    completed=78,
                    aired=79,
                    percent_completed=98.7,
                    next_episode=EpisodeSummary(trakt_id=700, season=4, number=7, title="The Mastermind's Identity"),
                ),
            )
            rows = repo.list_in_progress(session)
        self.assertEqual(rows[0].title, "That Time I Got Reincarnated as a Slime")

    def test_progress_repository_does_not_overwrite_real_title_with_fallback(self) -> None:
        repo = ProgressRepository()
        with self.db.session() as session:
            repo.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=135985,
                    title="That Time I Got Reincarnated as a Slime",
                    completed=77,
                    aired=79,
                    percent_completed=97.4,
                    next_episode=EpisodeSummary(trakt_id=699, season=4, number=6, title="Previous"),
                ),
            )
            repo.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=135985,
                    title="Show 135985",
                    completed=78,
                    aired=79,
                    percent_completed=98.7,
                    next_episode=EpisodeSummary(trakt_id=700, season=4, number=7, title="The Mastermind's Identity"),
                ),
            )
            rows = repo.list_in_progress(session)
        self.assertEqual(rows[0].title, "That Time I Got Reincarnated as a Slime")

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
            unseen_logs_before = repo.list_unseen(session)
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
            unseen_logs_after = repo.list_unseen(session)
        self.assertIsNotNone(first_log)
        self.assertIsNotNone(log)
        self.assertEqual(log.notify_count, 2)
        self.assertIsNotNone(log.seen_at)
        self.assertEqual([row.episode_trakt_id for row in unseen_logs_before], [70])
        self.assertEqual(unseen_logs_after, [])
        self.assertNotIn(70, unseen_ids)

    def test_database_uses_wal_mode_for_concurrent_reads(self) -> None:
        with self.db.session() as session:
            journal_mode = session.execute(text("PRAGMA journal_mode")).scalar()
            busy_timeout = session.execute(text("PRAGMA busy_timeout")).scalar()
        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertGreaterEqual(int(busy_timeout), 15000)

    def test_replace_show_episodes_preserves_resolved_enrich_metadata(self) -> None:
        repo = EpisodeRepository()
        with self.db.session() as session:
            row = repo.upsert_episode(
                session,
                101,
                EpisodeSummary(
                    trakt_id=501,
                    season=1,
                    number=2,
                    title="Episode 2",
                    trakt_rating=8.1,
                    trakt_votes=700,
                    imdb_id="tt123",
                    imdb_rating=8.4,
                    imdb_votes=1200,
                ),
            )
            row.still_url = "https://image.example/still.jpg"
            row.still_status = ENRICH_STATUS_READY
            row.trakt_details_status = ENRICH_STATUS_READY
            row.imdb_season = 2
            row.imdb_episode = 1
            row.imdb_coordinates_revision = "revision-1"
        with self.db.session() as session:
            repo.replace_show_episodes(
                session,
                101,
                [EpisodeSummary(trakt_id=501, season=1, number=2, title="Episode 2 refreshed")],
            )
            row = repo.find_episode(session, 101, 1, 2)
        self.assertEqual(row.still_url, "https://image.example/still.jpg")
        self.assertEqual(row.still_status, ENRICH_STATUS_READY)
        self.assertEqual(row.trakt_rating, 8.1)
        self.assertEqual(row.trakt_votes, 700)
        self.assertEqual(row.imdb_id, "tt123")
        self.assertEqual((row.imdb_season, row.imdb_episode), (2, 1))
        self.assertEqual(row.imdb_coordinates_revision, "revision-1")
        self.assertEqual(row.imdb_match_status, "resolved")
        self.assertEqual(row.trakt_details_status, ENRICH_STATUS_READY)

    def test_replace_show_episodes_preserves_no_match_key_until_title_changes(self) -> None:
        repo = EpisodeRepository()
        with self.db.session() as session:
            row = repo.upsert_episode(
                session,
                101,
                EpisodeSummary(trakt_id=501, season=1, number=2, title="Episode 2"),
            )
            row.imdb_match_status = "no_match"
            row.imdb_match_attempt_key = "attempt-key"
        with self.db.session() as session:
            repo.replace_show_episodes(
                session,
                101,
                [EpisodeSummary(trakt_id=501, season=1, number=2, title="Episode 2")],
            )
            unchanged = repo.find_episode(session, 101, 1, 2)
        self.assertEqual(unchanged.imdb_match_status, "no_match")
        self.assertEqual(unchanged.imdb_match_attempt_key, "attempt-key")

        with self.db.session() as session:
            repo.replace_show_episodes(
                session,
                101,
                [EpisodeSummary(trakt_id=501, season=1, number=2, title="A Real Title")],
            )
            changed = repo.find_episode(session, 101, 1, 2)
        self.assertEqual(changed.imdb_match_status, "unknown")
        self.assertEqual(changed.imdb_match_attempt_key, "")

    def test_upsert_title_preserves_existing_poster_and_ratings(self) -> None:
        repo = TitleRepository()
        with self.db.session() as session:
            row = repo.upsert_title(
                session,
                TitleSummary(
                    trakt_id=77,
                    title_type="show",
                    title="Example",
                    poster_url="https://image.example/poster.jpg",
                    trakt_rating=7.7,
                    trakt_votes=900,
                    imdb_id="tt777",
                    imdb_rating=8.3,
                    imdb_votes=10000,
                ),
            )
            row.poster_status = ENRICH_STATUS_READY
            row.ratings_status = ENRICH_STATUS_READY
        with self.db.session() as session:
            repo.upsert_title(
                session,
                TitleSummary(
                    trakt_id=77,
                    title_type="show",
                    title="Example refreshed",
                ),
            )
            row = repo.get_title(session, 77)
        self.assertEqual(row.poster_url, "https://image.example/poster.jpg")
        self.assertEqual(row.poster_status, ENRICH_STATUS_READY)
        self.assertEqual(row.trakt_rating, 7.7)
        self.assertEqual(row.imdb_rating, 8.3)
        self.assertEqual(row.ratings_status, ENRICH_STATUS_READY)

    def test_update_artwork_enrich_state_writes_refresh_timestamps(self) -> None:
        title_repo = TitleRepository()
        episode_repo = EpisodeRepository()
        with self.db.session() as session:
            title_repo.upsert_title(
                session,
                TitleSummary(
                    trakt_id=88,
                    title_type="show",
                    title="Poster target",
                ),
            )
            episode_repo.upsert_episode(
                session,
                88,
                EpisodeSummary(trakt_id=801, season=1, number=1, title="Pilot"),
            )
            title_repo.update_poster_enrich_state(
                session,
                88,
                status=ENRICH_STATUS_CHECKED_NO_DATA,
                poster_url="",
            )
            episode_repo.update_still_enrich_state(
                session,
                88,
                1,
                1,
                status=ENRICH_STATUS_CHECKED_NO_DATA,
                still_url="",
            )
            title_row = title_repo.get_title(session, 88)
            episode_row = episode_repo.find_episode(session, 88, 1, 1)
        self.assertIsNotNone(title_row.poster_refreshed_at)
        self.assertIsNotNone(episode_row.still_refreshed_at)

    def test_update_poster_enrich_state_preserves_existing_poster_on_empty_refresh(self) -> None:
        title_repo = TitleRepository()
        with self.db.session() as session:
            title_repo.upsert_title(
                session,
                TitleSummary(
                    trakt_id=89,
                    title_type="movie",
                    title="Poster target",
                    poster_url="https://image.example/poster.jpg",
                ),
            )
            title_repo.update_poster_enrich_state(
                session,
                89,
                status=ENRICH_STATUS_CHECKED_NO_DATA,
                poster_url="",
            )
            title_row = title_repo.get_title(session, 89)
        self.assertEqual(title_row.poster_url, "https://image.example/poster.jpg")
        self.assertEqual(title_row.poster_status, ENRICH_STATUS_READY)
        self.assertIsNotNone(title_row.poster_refreshed_at)

    def test_database_backfills_status_columns_from_existing_values(self) -> None:
        with self.db.session() as session:
            TitleRepository().upsert_title(
                session,
                TitleSummary(
                    trakt_id=1,
                    title_type="movie",
                    title="Seeded",
                    poster_url="https://image.example/poster.jpg",
                    trakt_rating=8.2,
                    trakt_votes=500,
                ),
            )
            EpisodeRepository().upsert_episode(
                session,
                1,
                EpisodeSummary(trakt_id=11, season=1, number=1, title="Pilot"),
            )
            session.execute(text("UPDATE titles SET poster_status = '', ratings_status = '' WHERE trakt_id = 1"))
            session.execute(text("UPDATE episodes_cache SET still_status = '', still_missing = 1 WHERE show_trakt_id = 1"))
            session.execute(text("UPDATE episodes_cache SET imdb_id = 'tt123', imdb_match_status = '' WHERE show_trakt_id = 1"))
        self.db.close()
        self.db = Database(Path(self.tmpdir.name) / "test.sqlite3")
        self.db.create_schema()
        with self.db.session() as session:
            poster_status = session.execute(text("SELECT poster_status FROM titles WHERE trakt_id = 1")).scalar()
            ratings_status = session.execute(text("SELECT ratings_status FROM titles WHERE trakt_id = 1")).scalar()
            poster_refreshed_at = session.execute(text("SELECT poster_refreshed_at FROM titles WHERE trakt_id = 1")).scalar()
            still_status = session.execute(text("SELECT still_status FROM episodes_cache WHERE show_trakt_id = 1")).scalar()
            still_refreshed_at = session.execute(text("SELECT still_refreshed_at FROM episodes_cache WHERE show_trakt_id = 1")).scalar()
            imdb_match_status = session.execute(text("SELECT imdb_match_status FROM episodes_cache WHERE show_trakt_id = 1")).scalar()
        self.assertEqual(poster_status, ENRICH_STATUS_READY)
        self.assertEqual(ratings_status, ENRICH_STATUS_READY)
        self.assertIsNotNone(poster_refreshed_at)
        self.assertEqual(still_status, ENRICH_STATUS_CHECKED_NO_DATA)
        self.assertIsNotNone(still_refreshed_at)
        self.assertEqual(imdb_match_status, "resolved")

    def test_existing_database_migration_is_backed_up_and_versioned_once(self) -> None:
        path = Path(self.tmpdir.name) / "test.sqlite3"
        self.db.close()
        connection = sqlite3.connect(path)
        try:
            connection.execute("DELETE FROM schema_migrations")
            connection.commit()
        finally:
            connection.close()
        self.db = Database(path)
        self.db.create_schema()

        backup = path.with_name(f"{path.name}.pre-migration.bak")
        self.assertTrue(backup.exists())
        first_backup_mtime = backup.stat().st_mtime_ns
        with self.db.session() as session:
            version = session.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
        self.assertEqual(version, LATEST_SCHEMA_VERSION)

        self.db.close()
        self.db = Database(path)
        self.db.create_schema()
        self.assertEqual(backup.stat().st_mtime_ns, first_backup_mtime)


if __name__ == "__main__":
    unittest.main()
