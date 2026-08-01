from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from sqlalchemy import text

from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.domain import (
    EpisodeSummary,
    ProgressSnapshot,
    ProgressSortMode,
    ProgressView,
    TitleSummary,
)
from trakt_tracker.infrastructure.trakt.client import TraktClient
from trakt_tracker.persistence.database import LATEST_SCHEMA_VERSION, Database
from trakt_tracker.persistence.repositories import (
    EpisodeRepository,
    NotificationRepository,
    ProgressRepository,
    SyncStateRepository,
    TitleRepository,
    UserStateRepository,
)


class _Auth:
    def __init__(self, client) -> None:
        self._client = client
        self.config = SimpleNamespace()

    def get_client(self):
        return self._client


class _EpisodeMetadata:
    @staticmethod
    def should_refresh_next_episode_details(_episode, _cached) -> bool:
        return False

    @staticmethod
    def attach_progress_episode_metadata(_session, _progress, *, enrich_imdb: bool) -> None:
        return None


class _HiddenClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.show_progress = ProgressSnapshot(
            trakt_id=2,
            title="Paused",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(
                trakt_id=202,
                season=1,
                number=2,
                title="Next",
                first_aired=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        )

    def get_paused_shows(self, *, limit: int, page: int):
        self.calls.append(("get_paused", limit, page))
        if page == 1:
            return [
                {"show": {"ids": {"trakt": trakt_id}}}
                for trakt_id in range(1000, 1100)
            ]
        if page == 2:
            return [
                {"show": {"ids": {"trakt": 2}}},
                {"show": {"ids": {"trakt": 4}}},
            ]
        return []

    def get_dropped_shows(self, *, limit: int, page: int):
        self.calls.append(("get_dropped", limit, page))
        if page == 1:
            return [
                {"show": {"ids": {"trakt": 3}}},
                {"show": {"ids": {"trakt": 4}}},
            ]
        return []

    def add_paused_show(self, trakt_id: int):
        self.calls.append(("add_paused", trakt_id))
        return {"added": {"shows": 1}}

    def remove_paused_show(self, trakt_id: int):
        self.calls.append(("remove_paused", trakt_id))
        return {"deleted": {"shows": 1}}

    def add_dropped_show(self, trakt_id: int):
        self.calls.append(("add_dropped", trakt_id))
        return {"added": {"shows": 1}}

    def remove_dropped_show(self, trakt_id: int):
        self.calls.append(("remove_dropped", trakt_id))
        return {"deleted": {"shows": 1}}

    def get_show_progress(self, trakt_id: int, *, use_cache: bool = True) -> ProgressSnapshot:
        self.calls.append(("get_progress", trakt_id, use_cache))
        return ProgressSnapshot(
            trakt_id=trakt_id,
            title=self.show_progress.title,
            completed=self.show_progress.completed,
            aired=self.show_progress.aired,
            percent_completed=self.show_progress.percent_completed,
            next_episode=self.show_progress.next_episode,
        )


class ProgressPauseBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.create_schema()
        self.titles = TitleRepository()
        self.states = UserStateRepository()
        self.progress = ProgressRepository()
        self.episodes = EpisodeRepository()
        self.notifications = NotificationRepository()

    def tearDown(self) -> None:
        self.db.close()
        self.tempdir.cleanup()

    def _add_progress(
        self,
        trakt_id: int,
        *,
        title: str,
        year: int | None = None,
        last_watched_at: datetime | None = None,
        next_aired_at: datetime | None = None,
    ) -> None:
        with self.db.session() as session:
            title_row = self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=trakt_id,
                    title_type="show",
                    title=title,
                    year=year,
                    poster_url=f"https://img.example/{trakt_id}.jpg",
                ),
            )
            state = self.states.ensure_state(session, title_row.id)
            state.tracked = True
            state.last_watched_at = last_watched_at
            self.progress.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=trakt_id,
                    title=title,
                    completed=1,
                    aired=2,
                    percent_completed=50.0,
                    next_episode=EpisodeSummary(
                        trakt_id=trakt_id * 100,
                        season=1,
                        number=2,
                        title="Next",
                        first_aired=next_aired_at,
                    ),
                ),
            )

    def _workflow(self, client) -> ProgressSyncWorkflow:
        return ProgressSyncWorkflow(
            self.db,
            _Auth(client),
            self.progress,
            self.episodes,
            self.titles,
            self.states,
            SyncStateRepository(),
            lambda _config: SimpleNamespace(),
            SimpleNamespace(),
            OperationLog(),
            _EpisodeMetadata(),
            notification_repo=self.notifications,
        )

    def test_state_views_apply_dropped_precedence_over_paused(self) -> None:
        for trakt_id in range(1, 5):
            self._add_progress(trakt_id, title=f"Show {trakt_id}")
        with self.db.session() as session:
            self.states.set_paused(session, 2, True)
            self.states.set_archived(session, 3, True)
            self.states.set_paused(session, 4, True)
            self.states.set_archived(session, 4, True)

            active = self.progress.list_in_progress(session, view=ProgressView.ACTIVE)
            paused = self.progress.list_in_progress(session, view=ProgressView.PAUSED)
            dropped = self.progress.list_in_progress(session, view=ProgressView.DROPPED)

        self.assertEqual([item.trakt_id for item in active], [1])
        self.assertEqual([item.trakt_id for item in paused], [2])
        self.assertEqual([item.trakt_id for item in dropped], [3, 4])
        self.assertTrue(dropped[1].is_paused)
        self.assertTrue(dropped[1].is_dropped)

    def test_progress_sorting_keeps_nulls_last_in_both_directions(self) -> None:
        self._add_progress(
            1,
            title="Beta",
            year=2010,
            last_watched_at=datetime(2024, 1, 1),
            next_aired_at=datetime(2023, 1, 1),
        )
        self._add_progress(
            2,
            title="Alpha",
            year=2020,
            last_watched_at=datetime(2025, 1, 1),
            next_aired_at=None,
        )
        self._add_progress(
            3,
            title="Charlie",
            year=None,
            last_watched_at=None,
            next_aired_at=datetime(2025, 1, 1),
        )
        self._add_progress(
            4,
            title="Aaron",
            year=2020,
            last_watched_at=datetime(2025, 1, 1),
            next_aired_at=datetime(2025, 1, 1),
        )

        with self.db.session() as session:
            last_desc = self.progress.list_in_progress(
                session,
                sort_mode=ProgressSortMode.LAST_WATCHED,
                descending=True,
            )
            last_asc = self.progress.list_in_progress(
                session,
                sort_mode=ProgressSortMode.LAST_WATCHED,
                descending=False,
            )
            episode_desc = self.progress.list_in_progress(
                session,
                sort_mode=ProgressSortMode.EPISODE_RELEASE,
                descending=True,
            )
            year_asc = self.progress.list_in_progress(
                session,
                sort_mode=ProgressSortMode.RELEASE_YEAR,
                descending=False,
            )

        self.assertEqual([item.trakt_id for item in last_desc], [4, 2, 1, 3])
        self.assertEqual([item.trakt_id for item in last_asc], [1, 4, 2, 3])
        self.assertEqual([item.trakt_id for item in episode_desc], [4, 3, 1, 2])
        self.assertEqual([item.trakt_id for item in year_asc], [1, 4, 2, 3])
        self.assertEqual(last_desc[0].last_watched_at, datetime(2025, 1, 1))
        self.assertEqual(year_asc[0].title_year, 2010)

    def test_progress_limit_is_applied_after_requested_sort(self) -> None:
        for offset in range(55):
            self._add_progress(
                100 + offset,
                title=f"Show {offset:02d}",
                year=2000 + offset,
            )

        with self.db.session() as session:
            rows = self.progress.list_in_progress(
                session,
                sort_mode=ProgressSortMode.RELEASE_YEAR,
                descending=True,
            )

        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0].title_year, 2054)
        self.assertEqual(rows[-1].title_year, 2005)

    def test_progress_repository_can_return_unlimited_rows_for_background_work(self) -> None:
        for offset in range(55):
            self._add_progress(
                100 + offset,
                title=f"Show {offset:02d}",
                year=2000 + offset,
            )

        with self.db.session() as session:
            rows = self.progress.list_in_progress(
                session,
                sort_mode=ProgressSortMode.RELEASE_YEAR,
                descending=True,
                limit=None,
            )

        self.assertEqual(len(rows), 55)
        self.assertEqual(rows[0].title_year, 2054)
        self.assertEqual(rows[-1].title_year, 2000)

    def test_hidden_sync_happens_before_policy_skip_and_keeps_paused_progress_synced(self) -> None:
        for trakt_id in range(1, 5):
            self._add_progress(
                trakt_id,
                title=f"Show {trakt_id}",
                next_aired_at=datetime(2025, 1, 1, tzinfo=UTC) if trakt_id == 2 else None,
            )
        client = _HiddenClient()
        workflow = self._workflow(client)
        workflow._can_skip_full_progress_sync = lambda: True

        paused = workflow.sync_progress(view=ProgressView.PAUSED)

        self.assertEqual([item.trakt_id for item in paused], [2])
        self.assertIn(("get_paused", 100, 2), client.calls)
        self.assertIn(("get_dropped", 100, 1), client.calls)
        self.assertFalse(any(call[0] == "get_progress" for call in client.calls))
        with self.db.session() as session:
            sync_ids = self.progress.list_sync_show_ids(
                session,
                view=ProgressView.ACTIVE,
                include_paused=True,
            )
            notification = self.notifications.get_log(session, 2, 200)
        self.assertEqual(set(sync_ids), {1, 2})
        self.assertIsNotNone(notification)
        self.assertIsNotNone(notification.seen_at)

    def test_remote_first_actions_preserve_pause_under_drop_and_acknowledge_episode(self) -> None:
        self._add_progress(2, title="Paused")
        client = _HiddenClient()
        workflow = self._workflow(client)
        snapshot = ProgressSnapshot(
            trakt_id=2,
            title="Paused",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(
                trakt_id=202,
                season=1,
                number=2,
                title="Next",
                first_aired=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        )

        workflow.pause_show(2, progress=snapshot)
        workflow.drop_show(2)
        workflow.undrop_show(2)

        with self.db.session() as session:
            state = self.states.progress_state(session, 2)
            notification = self.notifications.get_log(session, 2, 202)
        self.assertTrue(state.paused)
        self.assertFalse(state.archived)
        self.assertTrue(state.tracked)
        self.assertIsNotNone(notification)
        self.assertIsNotNone(notification.seen_at)
        self.assertEqual(
            [call for call in client.calls if call[0].startswith(("add_", "remove_"))],
            [("add_paused", 2), ("add_dropped", 2), ("remove_dropped", 2)],
        )

    def test_remote_failure_does_not_change_local_pause_state(self) -> None:
        self._add_progress(2, title="Paused")
        client = _HiddenClient()

        def fail(_trakt_id: int):
            raise RuntimeError("Trakt unavailable")

        client.add_paused_show = fail
        workflow = self._workflow(client)

        with self.assertRaisesRegex(RuntimeError, "Trakt unavailable"):
            workflow.pause_show(2)

        with self.db.session() as session:
            state = self.states.progress_state(session, 2)
        self.assertFalse(state.paused)

    def test_remote_remove_and_drop_failures_preserve_existing_local_state(self) -> None:
        self._add_progress(2, title="Paused")
        client = _HiddenClient()
        workflow = self._workflow(client)

        def fail(_trakt_id: int):
            raise RuntimeError("Trakt unavailable")

        client.add_dropped_show = fail
        with self.assertRaisesRegex(RuntimeError, "Trakt unavailable"):
            workflow.drop_show(2)
        with self.db.session() as session:
            state = self.states.progress_state(session, 2)
        self.assertFalse(state.archived)

        client.add_dropped_show = lambda trakt_id: {"added": {"shows": 1}}
        workflow.pause_show(2)
        client.remove_paused_show = fail
        with self.assertRaisesRegex(RuntimeError, "Trakt unavailable"):
            workflow.resume_show(2)
        with self.db.session() as session:
            state = self.states.progress_state(session, 2)
        self.assertTrue(state.paused)

        workflow.drop_show(2)
        client.remove_dropped_show = fail
        with self.assertRaisesRegex(RuntimeError, "Trakt unavailable"):
            workflow.undrop_show(2)
        with self.db.session() as session:
            state = self.states.progress_state(session, 2)
        self.assertTrue(state.archived)
        self.assertTrue(state.paused)

    def test_refresh_acknowledges_each_next_episode_while_show_is_paused(self) -> None:
        self._add_progress(2, title="Paused")
        with self.db.session() as session:
            self.states.set_paused(session, 2, True)
        client = _HiddenClient()
        workflow = self._workflow(client)

        refreshed = workflow.refresh_show_progress(2, fresh=True)

        self.assertTrue(refreshed.is_paused)
        with self.db.session() as session:
            notification = self.notifications.get_log(session, 2, 202)
        self.assertIsNotNone(notification)
        self.assertIsNotNone(notification.seen_at)

    def test_future_episode_is_acknowledged_only_after_it_releases_during_pause(self) -> None:
        self._add_progress(2, title="Paused")
        client = _HiddenClient()
        future = ProgressSnapshot(
            trakt_id=2,
            title="Paused",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(
                trakt_id=203,
                season=1,
                number=3,
                title="Future",
                first_aired=datetime(2099, 1, 1, tzinfo=UTC),
            ),
        )
        workflow = self._workflow(client)

        workflow.pause_show(2, progress=future)
        with self.db.session() as session:
            self.assertIsNone(self.notifications.get_log(session, 2, 203))

        client.show_progress = ProgressSnapshot(
            trakt_id=2,
            title="Paused",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(
                trakt_id=203,
                season=1,
                number=3,
                title="Future",
                first_aired=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        )
        workflow.refresh_show_progress(2, fresh=True)

        with self.db.session() as session:
            notification = self.notifications.get_log(session, 2, 203)
        self.assertIsNotNone(notification)
        self.assertIsNotNone(notification.seen_at)


class ProgressPauseMigrationTests(unittest.TestCase):
    def test_v4_database_migrates_to_latest_schema_with_explicit_paused_column(self) -> None:
        with TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE user_title_state ("
                    "id INTEGER PRIMARY KEY, title_id INTEGER NOT NULL, in_history BOOLEAN DEFAULT 0, "
                    "tracked BOOLEAN DEFAULT 0, pinned BOOLEAN DEFAULT 0, archived BOOLEAN DEFAULT 0, "
                    "rating INTEGER, last_watched_at DATETIME, last_synced_at DATETIME)"
                )
                connection.execute(
                    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (4, '2026-01-01T00:00:00Z')"
                )
                connection.commit()
            finally:
                connection.close()

            db = Database(path)
            try:
                db.create_schema()
                with db.session() as session:
                    columns = {
                        str(row[1])
                        for row in session.execute(text('PRAGMA table_info("user_title_state")'))
                    }
                    version = session.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
            finally:
                db.close()

        self.assertIn("paused", columns)
        self.assertEqual(version, LATEST_SCHEMA_VERSION)
        self.assertEqual(LATEST_SCHEMA_VERSION, 7)


class TraktHiddenClientTests(unittest.TestCase):
    def test_hidden_show_reads_use_cache_and_mutations_use_show_payload(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        calls: list[tuple] = []
        client._request = lambda method, path, **kwargs: calls.append((method, path, kwargs)) or []

        client.get_paused_shows(limit=50, page=2)
        client.get_dropped_shows(limit=25, page=3)
        client.add_paused_show(7)
        client.remove_paused_show(7)
        client.add_dropped_show(8)
        client.remove_dropped_show(8)

        self.assertEqual(calls[0][0:2], ("GET", "/users/hidden/progress_watched"))
        self.assertEqual(calls[0][2]["params"], {"type": "show", "limit": 50, "page": 2})
        self.assertTrue(calls[0][2]["use_cache"])
        self.assertEqual(calls[1][0:2], ("GET", "/users/hidden/dropped"))
        self.assertEqual(calls[1][2]["params"], {"type": "show", "limit": 25, "page": 3})
        self.assertTrue(calls[1][2]["use_cache"])
        self.assertEqual(
            [(method, path, kwargs["json"]) for method, path, kwargs in calls[2:]],
            [
                ("POST", "/users/hidden/progress_watched", {"shows": [{"ids": {"trakt": 7}}]}),
                ("POST", "/users/hidden/progress_watched/remove", {"shows": [{"ids": {"trakt": 7}}]}),
                ("POST", "/users/hidden/dropped", {"shows": [{"ids": {"trakt": 8}}]}),
                ("POST", "/users/hidden/dropped/remove", {"shows": [{"ids": {"trakt": 8}}]}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
