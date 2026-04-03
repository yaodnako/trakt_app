from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trakt_tracker.application.catalog import CatalogService
from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
)
from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.history import HistoryService
from trakt_tracker.application.history_read_model import HistoryReadModelService
from trakt_tracker.application.interactions import InteractionService
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.services import build_services
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.domain import EpisodeSummary, HistoryItemInput, ProgressSnapshot, RatingInput, TitleSummary
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import EpisodeRepository, HistoryRepository, SyncStateRepository, TitleRepository, UserStateRepository


class _FakeConfig:
    tmdb_api_key = ""
    tmdb_read_access_token = ""


class _FakeAuthService:
    def __init__(self, client) -> None:
        self._client = client
        self.config = _FakeConfig()

    def get_client(self):
        return self._client


class _FakeTraktClient:
    def __init__(self) -> None:
        self.searched: list[tuple[str, str | None]] = []
        self.history_items: list[HistoryItemInput] = []
        self.ratings: list[RatingInput] = []
        self.episode_details_calls: list[tuple[int, int, int]] = []
        self.title_details_calls: list[tuple[int, str]] = []
        self.title_details = TitleSummary(
            trakt_id=11,
            title_type="movie",
            title="Dune",
            overview="Spice.",
            poster_url="//poster.example/dune.jpg",
        )

    def search_titles(self, query: str, title_type: str | None = None) -> list[TitleSummary]:
        self.searched.append((query, title_type))
        return [
            TitleSummary(
                trakt_id=11,
                title_type="movie",
                title="Dune",
                poster_url="//poster.example/dune.jpg",
                imdb_votes=100,
            )
        ]

    def get_title_details(self, trakt_id: int, title_type: str) -> TitleSummary:
        self.title_details_calls.append((trakt_id, title_type))
        return replace(self.title_details, trakt_id=trakt_id, title_type=title_type)

    def add_history_item(self, item: HistoryItemInput) -> None:
        self.history_items.append(item)

    def set_rating(self, item: RatingInput) -> None:
        self.ratings.append(item)

    def get_show_episodes(self, trakt_id: int) -> list[EpisodeSummary]:
        return [
            EpisodeSummary(trakt_id=301, season=1, number=1, title="Pilot"),
            EpisodeSummary(trakt_id=302, season=1, number=2, title="Second"),
        ]

    def get_episode_details(self, show_trakt_id: int, season: int, episode: int) -> EpisodeSummary:
        self.episode_details_calls.append((show_trakt_id, season, episode))
        return EpisodeSummary(
            trakt_id=300 + episode,
            season=season,
            number=episode,
            title=f"Episode {episode}",
            trakt_rating=7.9,
            trakt_votes=321,
        )


class _FakeTmdbClient:
    def is_configured(self) -> bool:
        return True

    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        return replace(title, poster_url="https://tmdb.example/poster.jpg", status="released")


class _FakeImdbClient:
    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        return replace(title, imdb_rating=8.5, imdb_votes=12345)

    def is_ready(self) -> bool:
        return False


class _FakeHistoryService:
    def __init__(self) -> None:
        self.items: list[HistoryItemInput] = []
        self.ratings: list[tuple[RatingInput, str]] = []

    def add_history_item(self, item: HistoryItemInput) -> None:
        self.items.append(item)

    def set_rating(self, item: RatingInput, title: str = "") -> None:
        self.ratings.append((item, title))

    def displayed_history_rating(
        self,
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> int | None:
        for item, _title in reversed(self.ratings):
            if item.title_type != title_type or item.trakt_id != trakt_id:
                continue
            if item.season != season or item.episode != episode:
                continue
            return item.rating
        return None


class _FakeNotificationService:
    def __init__(self) -> None:
        self.seen: list[tuple[int, str, EpisodeSummary]] = []

    def mark_episode_seen(self, *, show_trakt_id: int, show_title: str, episode: EpisodeSummary) -> None:
        self.seen.append((show_trakt_id, show_title, episode))


class _FakeProgressService:
    def __init__(self) -> None:
        self.dropped: list[tuple[int, bool]] = []

    def drop_show(self, trakt_id: int) -> None:
        self.dropped.append((trakt_id, True))

    def undrop_show(self, trakt_id: int) -> None:
        self.dropped.append((trakt_id, False))


class ApplicationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmpdir.name) / "test.sqlite3")
        self.db.create_schema()
        self.titles = TitleRepository()
        self.user_states = UserStateRepository()
        self.history_repo = HistoryRepository()
        self.sync_state = SyncStateRepository()
        self.episode_repo = EpisodeRepository()
        self.trakt_client = _FakeTraktClient()
        self.auth = _FakeAuthService(self.trakt_client)
        self.imdb = _FakeImdbClient()
        self.episode_metadata = EpisodeMetadataService(self.db, self.episode_repo, self.imdb, self.titles, self.auth, lambda _config: _FakeTmdbClient())
        self.history_read_model = HistoryReadModelService(
            self.db,
            self.history_repo,
            self.user_states,
            self.titles,
            self.episode_repo,
            self.episode_metadata,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def test_catalog_service_search_persists_history_and_saved_state(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )

        results = service.search_titles("Dune")
        self.assertEqual(len(results), 1)
        self.assertEqual(self.trakt_client.searched, [("Dune", None)])
        self.assertEqual(service.search_history(), ["Dune"])

        state = service.load_last_search_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["query"], "Dune")
        self.assertEqual(state["results"][0].poster_url, "https://poster.example/dune.jpg")

    def test_history_service_add_and_rate_movie_updates_display_rating(self) -> None:
        service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )

        watched_at = datetime.now(tz=UTC)
        service.add_history_item(
            HistoryItemInput(
                title_type="movie",
                trakt_id=77,
                watched_at=watched_at,
                title="Arrival",
            )
        )
        service.set_rating(
            RatingInput(title_type="movie", trakt_id=77, rating=9),
            title="Arrival",
        )

        self.assertEqual(len(self.trakt_client.history_items), 1)
        self.assertEqual(len(self.trakt_client.ratings), 1)
        self.assertEqual(
            service.displayed_history_rating(title_type="movie", trakt_id=77),
            9,
        )
        self.assertEqual(service.history_titles(title_type="movie"), ["Arrival"])

    def test_interaction_service_marks_progress_episode_watched(self) -> None:
        history = _FakeHistoryService()
        notifications = _FakeNotificationService()
        progress = _FakeProgressService()
        service = InteractionService(history, notifications, progress)
        snapshot = ProgressSnapshot(
            trakt_id=5,
            title="Severance",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(trakt_id=55, season=2, number=3, title="Who Is Alive?"),
        )

        result = service.mark_progress_episode_watched(snapshot, watched_at=datetime(2026, 4, 1, tzinfo=UTC))

        self.assertEqual(result.trakt_id, 5)
        self.assertEqual(result.season, 2)
        self.assertEqual(result.episode, 3)
        self.assertEqual(len(history.items), 1)
        self.assertEqual(len(notifications.seen), 1)

    def test_interaction_service_rejects_future_seen_mark(self) -> None:
        history = _FakeHistoryService()
        notifications = _FakeNotificationService()
        progress = _FakeProgressService()
        service = InteractionService(history, notifications, progress)
        snapshot = ProgressSnapshot(
            trakt_id=9,
            title="Andor",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(
                trakt_id=99,
                season=1,
                number=4,
                title="Aldhani",
                first_aired=datetime.now(tz=UTC) + timedelta(days=1),
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "aired yet"):
            service.mark_progress_episode_seen(snapshot, now=datetime.now(tz=UTC))

    def test_sync_service_auto_imdb_interval_defaults_to_three_hours(self) -> None:
        self.assertEqual(AppConfig().imdb_auto_sync_interval_hours, 3)

    def test_sync_service_auto_imdb_sync_runs_once_per_interval(self) -> None:
        config_store = ConfigStore(Path(self.tmpdir.name) / "config.json")
        services = build_services(config_store, self.db)
        sync_calls: list[bool] = []

        services.sync._imdb_client.sync = lambda force=False, status_callback=None: sync_calls.append(force) or True
        services.sync._episode_metadata.backfill_episode_imdb_ids_from_payloads = lambda payloads: None
        services.sync._episode_metadata.enrich_episode_imdb_ratings = lambda: None

        self.assertTrue(services.sync.should_auto_sync_imdb_dataset(3))
        self.assertTrue(services.sync.maybe_sync_imdb_dataset(3))
        self.assertFalse(services.sync.should_auto_sync_imdb_dataset(3))
        self.assertFalse(services.sync.maybe_sync_imdb_dataset(3))
        self.assertEqual(sync_calls, [False])

    def test_history_service_enriches_visible_episode_details_only_when_missing(self) -> None:
        service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )
        with self.db.session() as session:
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=301, season=3, number=4, title="Kill Switch"),
            )
        changed = service.enrich_visible_episode_details(
            [{"title_trakt_id": 138748, "type": "show", "season": 3, "episode": 4}]
        )
        self.assertTrue(changed)
        self.assertEqual(self.trakt_client.episode_details_calls, [(138748, 3, 4)])
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 138748, 3, 4)
            self.assertEqual(row.trakt_rating, 7.9)
            self.assertEqual(row.trakt_votes, 321)
            self.assertEqual(row.trakt_details_status, ENRICH_STATUS_READY)

    def test_history_service_skips_episode_refetch_after_checked_no_data(self) -> None:
        service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )
        with self.db.session() as session:
            row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=301, season=3, number=4, title="Kill Switch"),
            )
            row.trakt_details_status = ENRICH_STATUS_CHECKED_NO_DATA
        changed = service.enrich_visible_episode_details(
            [{"title_trakt_id": 138748, "type": "show", "season": 3, "episode": 4}]
        )
        self.assertFalse(changed)
        self.assertEqual(self.trakt_client.episode_details_calls, [])

    def test_history_service_has_missing_visible_episode_details_ignores_resolved_states(self) -> None:
        service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )
        with self.db.session() as session:
            row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=301, season=3, number=4, title="Kill Switch"),
            )
            row.trakt_details_status = ENRICH_STATUS_CHECKED_NO_DATA
            row.still_status = ENRICH_STATUS_CHECKED_NO_DATA
            row.still_missing = True
        self.assertFalse(
            service.has_missing_visible_episode_details(
                [{"title_trakt_id": 138748, "type": "show", "season": 3, "episode": 4}]
            )
        )

    def test_catalog_service_has_missing_visible_titles_ignores_resolved_states(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        rows = [
            {
                "title_trakt_id": 11,
                "type": "movie",
                "poster_url": "",
                "title_poster_status": ENRICH_STATUS_CHECKED_NO_DATA,
                "title_trakt_rating": None,
                "title_trakt_votes": None,
                "title_ratings_status": ENRICH_STATUS_CHECKED_NO_DATA,
            }
        ]
        self.assertFalse(service.has_missing_visible_titles(rows))

    def test_catalog_service_queues_title_ratings_when_trakt_exists_but_imdb_unresolved(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        rows = [
            {
                "title_trakt_id": 11,
                "type": "show",
                "poster_url": "https://poster.example/show.jpg",
                "title_poster_status": ENRICH_STATUS_READY,
                "title_trakt_rating": 8.2,
                "title_trakt_votes": 1000,
                "title_imdb_rating": None,
                "title_imdb_votes": None,
                "title_ratings_status": "unknown",
            }
        ]
        self.assertEqual(service.select_title_enrich_keys(rows), [(11, "show")])

    def test_history_set_rating_invalidates_episode_trakt_status(self) -> None:
        service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )
        with self.db.session() as session:
            row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(
                    trakt_id=301,
                    season=3,
                    number=4,
                    title="Kill Switch",
                    trakt_rating=7.9,
                    trakt_votes=321,
                ),
            )
            row.trakt_details_status = ENRICH_STATUS_READY
        service.set_rating(
            RatingInput(title_type="show", trakt_id=138748, rating=9, season=3, episode=4),
            title="The Capture",
        )
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 138748, 3, 4)
            self.assertEqual(row.trakt_details_status, "unknown")

    def test_progress_dashboard_uses_stored_metadata_only_without_network_enrich(self) -> None:
        from trakt_tracker.persistence.repositories import ProgressRepository

        workflow = ProgressSyncWorkflow(
            self.db,
            self.auth,
            ProgressRepository(),
            self.episode_repo,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=138748,
                    title_type="show",
                    title="The Capture",
                    poster_url="https://poster.example/capture.jpg",
                    status="returning",
                ),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(
                    trakt_id=301,
                    season=3,
                    number=4,
                    title="Kill Switch",
                    still_url="https://still.example/capture.jpg",
                    trakt_rating=7.9,
                    trakt_votes=321,
                    imdb_id="tt123",
                    imdb_rating=8.1,
                    imdb_votes=106,
                ),
            )
            ProgressRepository().upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=138748,
                    title="The Capture",
                    completed=3,
                    aired=6,
                    percent_completed=50.0,
                    next_episode=EpisodeSummary(trakt_id=301, season=3, number=4, title="Kill Switch"),
                ),
            )
        items = workflow.dashboard_progress()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].poster_url, "https://poster.example/capture.jpg")
        self.assertEqual(items[0].next_episode.still_url, "https://still.example/capture.jpg")
        self.assertEqual(self.trakt_client.title_details_calls, [])
        self.assertEqual(self.trakt_client.episode_details_calls, [])


if __name__ == "__main__":
    unittest.main()
