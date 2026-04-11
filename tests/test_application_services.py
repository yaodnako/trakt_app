from __future__ import annotations

import tempfile
import time
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
from trakt_tracker.application.episode_ratings_matrix import EpisodeRatingsMatrixService, rating_bucket_color
from trakt_tracker.application.enrich_queue import EnrichQueueService
from trakt_tracker.application.history import HistoryService
from trakt_tracker.application.history_read_model import HistoryReadModelService
from trakt_tracker.application.history_sync import HistorySyncWorkflow
from trakt_tracker.application.interactions import InteractionService
from trakt_tracker.application.metadata_refresh_policy import TRIGGER_PAGE_CONTEXT, TRIGGER_VIEWPORT
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.services import SyncService, build_services
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.domain import EpisodeSummary, HistoryItemInput, ProgressSnapshot, RatingInput, TitleSummary
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import EpisodeRepository, HistoryRepository, ProgressRepository, SyncStateRepository, TitleRepository, UserStateRepository


class _FakeConfig:
    tmdb_api_key = ""
    tmdb_read_access_token = ""


class _FakeAuthService:
    def __init__(self, client) -> None:
        self._client = client
        self.config = _FakeConfig()

    def get_client(self):
        return self._client

    def is_authorized(self) -> bool:
        return True


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

    def get_title_details(self, trakt_id: int, title_type: str, use_cache: bool = True) -> TitleSummary:
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

    def get_episode_details(self, show_trakt_id: int, season: int, episode: int, use_cache: bool = True) -> EpisodeSummary:
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

    def get_episode_still_url(self, show_tmdb_id: int, season: int, episode: int) -> str:
        return "https://tmdb.example/still.jpg"


class _FakeImdbClient:
    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        return replace(title, imdb_rating=8.5, imdb_votes=12345)

    def is_ready(self) -> bool:
        return False

    def enrich_episode(self, episode: EpisodeSummary) -> EpisodeSummary:
        return episode

    def lookup_episode_imdb_id(self, show_imdb_id: str, season_number: int, episode_number: int) -> str:
        return ""

    def lookup_episode_imdb_id_by_title(self, show_imdb_id: str, episode_title: str) -> str:
        return ""


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

    def test_history_sync_fetches_movie_watch_history_stream(self) -> None:
        class _HistoryClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str | None, int]] = []

            def get_watch_history(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict]:
                self.calls.append((title_type, page))
                if title_type is None:
                    return [
                        {
                            "id": 10,
                            "type": "episode",
                            "watched_at": "2026-04-09T15:54:00.000Z",
                            "show": {"ids": {"trakt": 1}, "title": "Show"},
                            "episode": {"ids": {"trakt": 101}, "season": 1, "number": 1, "title": "Pilot"},
                        }
                    ]
                if title_type == "movie":
                    return [
                        {
                            "id": 20,
                            "type": "movie",
                            "watched_at": "2026-04-10T12:42:32.000Z",
                            "movie": {"ids": {"trakt": 2}, "title": "Movie"},
                        }
                    ]
                return []

        client = _HistoryClient()

        items = HistorySyncWorkflow._fetch_all_watch_history(client)

        self.assertEqual([item["type"] for item in items], ["episode", "movie"])
        self.assertIn((None, 1), client.calls)
        self.assertIn(("movie", 1), client.calls)

    def test_history_sync_dedupes_movie_if_all_stream_includes_it(self) -> None:
        class _HistoryClient:
            def get_watch_history(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict]:
                movie = {
                    "id": 20,
                    "type": "movie",
                    "watched_at": "2026-04-10T12:42:32.000Z",
                    "movie": {"ids": {"trakt": 2}, "title": "Movie"},
                }
                if title_type is None:
                    return [movie]
                if title_type == "movie":
                    return [movie]
                return []

        items = HistorySyncWorkflow._fetch_all_watch_history(_HistoryClient())

        self.assertEqual([item["id"] for item in items], [20])

    def test_history_title_summaries_aggregate_by_latest_watch(self) -> None:
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=1, title_type="show", title="Severance"))
            self.titles.upsert_title(session, TitleSummary(trakt_id=2, title_type="movie", title="Dune"))
            self.history_repo.add_event(
                session,
                trakt_history_id=101,
                title_trakt_id=1,
                title="Severance",
                title_type="show",
                action="watched",
                watched_at=datetime(2026, 4, 4, 12, 0, tzinfo=UTC),
                season=1,
                episode=2,
                rating=6,
            )
            self.history_repo.add_event(
                session,
                trakt_history_id=100,
                title_trakt_id=1,
                title="Severance",
                title_type="show",
                action="watched",
                watched_at=datetime(2026, 4, 3, 12, 0, tzinfo=UTC),
                season=1,
                episode=1,
                rating=8,
            )
            self.history_repo.add_event(
                session,
                trakt_history_id=200,
                title_trakt_id=2,
                title="Dune",
                title_type="movie",
                action="watched",
                watched_at=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
                rating=9,
            )

        rows = self.history_read_model.history_title_summaries()

        self.assertEqual([row["title_key"] for row in rows], ["show:1", "movie:2"])
        self.assertEqual(rows[0]["watched_count"], 2)
        self.assertEqual(rows[0]["my_rating"], 7.0)
        self.assertEqual(rows[0]["latest_season"], 1)
        self.assertEqual(rows[0]["latest_episode"], 2)
        self.assertEqual(rows[1]["my_rating"], 9)

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
        self.assertEqual(sync_calls, [True])

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
            row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
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
            row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
            row.still_status = ENRICH_STATUS_CHECKED_NO_DATA
            row.still_missing = True
            row.still_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        self.assertFalse(
            service.has_missing_visible_episode_details(
                [{"title_trakt_id": 138748, "type": "show", "season": 3, "episode": 4}]
            )
        )

    def test_enrich_episode_stills_empty_result_marks_checked_no_data(self) -> None:
        class _EmptyStillTmdbClient(_FakeTmdbClient):
            def get_episode_still_url(self, show_tmdb_id: int, season: int, episode: int) -> str:
                return ""

        episode_metadata = EpisodeMetadataService(
            self.db,
            self.episode_repo,
            self.imdb,
            self.titles,
            self.auth,
            lambda _config: _EmptyStillTmdbClient(),
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=138748,
                    title_type="show",
                    title="The Capture",
                    tmdb_id=250487,
                ),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=301, season=2, number=6, title="Episode 6"),
            )
        changed = episode_metadata.enrich_episode_stills(
            [(138748, 2, 6)],
            trigger="manual_repair",
            requested_parts=("still",),
        )
        self.assertFalse(changed)
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 138748, 2, 6)
            self.assertEqual(row.still_status, ENRICH_STATUS_CHECKED_NO_DATA)

    def test_enrich_episode_stills_missing_show_tmdb_id_marks_checked_no_data(self) -> None:
        class _EmptyStillTmdbClient(_FakeTmdbClient):
            def get_episode_still_url(self, show_tmdb_id: int, season: int, episode: int) -> str:
                return ""

        episode_metadata = EpisodeMetadataService(
            self.db,
            self.episode_repo,
            self.imdb,
            self.titles,
            self.auth,
            lambda _config: _EmptyStillTmdbClient(),
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=138748,
                    title_type="show",
                    title="The Capture",
                ),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=301, season=2, number=6, title="Episode 6"),
            )
        changed = episode_metadata.enrich_episode_stills(
            [(138748, 2, 6)],
            trigger="manual_repair",
            requested_parts=("still",),
        )
        self.assertFalse(changed)
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 138748, 2, 6)
            self.assertEqual(row.still_status, ENRICH_STATUS_CHECKED_NO_DATA)

    def test_recent_released_checked_no_data_still_requeues_for_viewport(self) -> None:
        with self.db.session() as session:
            row = self.episode_repo.upsert_episode(
                session,
                139960,
                EpisodeSummary(
                    trakt_id=12138429,
                    season=5,
                    number=1,
                    title="Fifteen Inches of Sheer Dynamite",
                    first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                ),
            )
            row.still_status = ENRICH_STATUS_CHECKED_NO_DATA
            row.still_missing = True
            row.still_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=6)
        self.assertTrue(
            self.episode_metadata.episode_key_needs_enrich(
                139960,
                5,
                1,
                trigger=TRIGGER_VIEWPORT,
                requested_parts=("still",),
            )
        )

    def test_recent_released_checked_no_data_still_requeues_for_page_context(self) -> None:
        with self.db.session() as session:
            row = self.episode_repo.upsert_episode(
                session,
                139960,
                EpisodeSummary(
                    trakt_id=12138430,
                    season=5,
                    number=2,
                    title="Teenage Kix",
                    first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                ),
            )
            row.still_status = ENRICH_STATUS_CHECKED_NO_DATA
            row.still_missing = True
            row.still_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=61)
        self.assertTrue(
            self.episode_metadata.episode_key_needs_enrich(
                139960,
                5,
                2,
                trigger=TRIGGER_PAGE_CONTEXT,
                requested_parts=("still",),
            )
        )

    def test_old_checked_no_data_still_does_not_requeue_outside_recent_window(self) -> None:
        with self.db.session() as session:
            row = self.episode_repo.upsert_episode(
                session,
                139960,
                EpisodeSummary(
                    trakt_id=12138431,
                    season=4,
                    number=1,
                    title="Old Episode",
                    first_aired=datetime.now(tz=UTC) - timedelta(days=30),
                ),
            )
            row.still_status = ENRICH_STATUS_CHECKED_NO_DATA
            row.still_missing = True
            row.still_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=2)
        self.assertFalse(
            self.episode_metadata.episode_key_needs_enrich(
                139960,
                4,
                1,
                trigger=TRIGGER_PAGE_CONTEXT,
                requested_parts=("still",),
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
                "title_poster_refreshed_at": datetime.now(tz=UTC),
                "backdrop_url": "",
                "title_backdrop_status": ENRICH_STATUS_CHECKED_NO_DATA,
                "title_backdrop_refreshed_at": datetime.now(tz=UTC),
                "title_trakt_rating": None,
                "title_trakt_votes": None,
                "title_ratings_status": ENRICH_STATUS_CHECKED_NO_DATA,
                "title_ratings_refreshed_at": datetime.now(tz=UTC),
            }
        ]
        self.assertFalse(service.has_missing_visible_titles(rows))

    def test_catalog_service_queues_movie_backdrop_when_unresolved(self) -> None:
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
                "poster_url": "https://poster.example/movie.jpg",
                "title_poster_status": ENRICH_STATUS_READY,
                "backdrop_url": "",
                "title_backdrop_status": "unknown",
                "title_trakt_rating": 8.2,
                "title_trakt_votes": 1000,
                "title_imdb_rating": 7.9,
                "title_imdb_votes": 10000,
                "title_ratings_status": ENRICH_STATUS_READY,
            }
        ]
        self.assertEqual(service.select_title_enrich_keys(rows), [(11, "movie")])

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

    def test_episode_ratings_matrix_builds_grid_and_season_avgs(self) -> None:
        self.trakt_client.get_show_episodes = lambda trakt_id: [
            EpisodeSummary(trakt_id=101, season=1, number=1, title="E1", imdb_rating=9.2, imdb_votes=1134),
            EpisodeSummary(trakt_id=102, season=1, number=2, title="E2"),
            EpisodeSummary(trakt_id=201, season=2, number=1, title="E1", imdb_rating=7.4, imdb_votes=90),
        ]
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=138748,
                    title_type="show",
                    title="The Capture",
                    imdb_id="tt8201186",
                ),
            )
        matrix = service.load_show_matrix(138748)
        self.assertTrue(matrix.has_episodes)
        self.assertEqual([season.label for season in matrix.seasons], ["S1", "S2", "ALL"])
        self.assertEqual([row.label for row in matrix.rows], ["E1", "E2"])
        self.assertEqual(matrix.rows[0].cells[0].display_value, "9.2")
        self.assertEqual(matrix.rows[0].cells[0].color, rating_bucket_color(9.2))
        self.assertEqual(matrix.rows[0].cells[1].display_value, "7.4")
        self.assertEqual(matrix.rows[1].cells[0].display_value, "?")
        self.assertEqual(matrix.rows[1].cells[0].state, "unrated")
        self.assertEqual(matrix.rows[1].cells[1].state, "empty")
        self.assertEqual(matrix.rows[0].cells[0].tooltip, "E1\nS01 E01\n1 134 votes")
        self.assertEqual([season.avg_display for season in matrix.seasons], ["9.2", "7.4", "8.3"])

    def test_episode_ratings_matrix_builds_trakt_values_and_averages(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=101, season=1, number=1, title="E1", trakt_rating=8.2, trakt_votes=2300, imdb_rating=9.2, imdb_votes=1134),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=102, season=1, number=2, title="E2", trakt_rating=6.8, trakt_votes=1200, imdb_rating=7.2, imdb_votes=900),
            )
        matrix = service.load_show_matrix(138748, provider="trakt")
        self.assertEqual(matrix.provider, "trakt")
        self.assertEqual(matrix.rows[0].cells[0].display_value, "8.2")
        self.assertEqual(matrix.rows[0].cells[0].trakt_display_value, "8.2")
        self.assertEqual(matrix.rows[0].cells[0].trakt_color, rating_bucket_color(8.2))
        self.assertEqual(matrix.rows[0].cells[0].tooltip, "E1\nS01 E01\n2 300 votes")
        self.assertEqual([season.avg_display for season in matrix.seasons], ["7.5", "7.5"])

    def test_episode_ratings_matrix_refreshes_missing_trakt_values(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=101, season=1, number=1, title="E1", imdb_rating=9.2, imdb_votes=1134),
            )
        matrix = service.load_show_matrix(138748, provider="trakt", refresh_missing=True)
        self.assertEqual(self.trakt_client.episode_details_calls, [(138748, 1, 1)])
        self.assertEqual(matrix.rows[0].cells[0].display_value, "7.9")
        self.assertEqual(matrix.rows[0].cells[0].trakt_votes, 321)

    def test_episode_ratings_matrix_refreshes_stale_ready_trakt_values(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=101, season=1, number=1, title="E1", trakt_rating=8.2, trakt_votes=2300),
            )
            row.trakt_details_status = ENRICH_STATUS_READY
            row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=6)
        matrix = service.load_show_matrix(138748, provider="trakt", refresh_missing=True)
        self.assertEqual(self.trakt_client.episode_details_calls, [(138748, 1, 1)])
        self.assertEqual(matrix.rows[0].cells[0].display_value, "7.9")
        self.assertEqual(matrix.rows[0].cells[0].trakt_votes, 321)

    def test_episode_ratings_matrix_does_not_refresh_fresh_ready_trakt_values(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=101, season=1, number=1, title="E1", trakt_rating=8.2, trakt_votes=2300),
            )
            row.trakt_details_status = ENRICH_STATUS_READY
            row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        matrix = service.load_show_matrix(138748, provider="trakt", refresh_missing=True)
        self.assertEqual(self.trakt_client.episode_details_calls, [])
        self.assertEqual(matrix.rows[0].cells[0].display_value, "8.2")
        self.assertEqual(matrix.rows[0].cells[0].trakt_votes, 2300)

    def test_sync_service_enqueues_due_background_trakt_episode_ratings(self) -> None:
        started: list[str] = []

        def handler(task) -> str:
            started.append(task.task_key)
            return "ready"

        queue = EnrichQueueService({"history_episode": handler}, max_workers=1)
        service = SyncService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            OperationLog(),
            self.episode_metadata,
            None,
            queue,
        )
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            due_row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=101, season=1, number=1, title="E1", trakt_rating=8.2, trakt_votes=2300, first_aired=datetime.now(tz=UTC) - timedelta(days=30)),
            )
            due_row.trakt_details_status = ENRICH_STATUS_READY
            due_row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=7)
            fresh_row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=102, season=1, number=2, title="E2", trakt_rating=8.1, trakt_votes=1200, first_aired=datetime.now(tz=UTC) - timedelta(days=5)),
            )
            fresh_row.trakt_details_status = ENRICH_STATUS_READY
            fresh_row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=7)
        queued = service.enqueue_due_background_trakt_episode_ratings(limit=10)
        self.assertEqual(queued, 1)
        deadline = datetime.now(tz=UTC) + timedelta(seconds=2)
        while len(started) < 1 and datetime.now(tz=UTC) < deadline:
            time.sleep(0.01)
        self.assertEqual(started, ["episode:138748:1:1"])

    def test_episode_ratings_matrix_overall_excludes_season_zero(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=11, season=0, number=1, title="Special", trakt_rating=0.0, trakt_votes=5, imdb_rating=0.0, imdb_votes=5),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=12, season=1, number=1, title="Main", trakt_rating=7.2, trakt_votes=500, imdb_rating=7.0, imdb_votes=400),
            )
        trakt_matrix = service.load_show_matrix(138748, provider="trakt")
        imdb_matrix = service.load_show_matrix(138748, provider="imdb")
        self.assertEqual(trakt_matrix.seasons[-1].label, "ALL")
        self.assertEqual(trakt_matrix.seasons[-1].avg_display, "7.2")
        self.assertEqual(imdb_matrix.seasons[-1].avg_display, "7.0")

    def test_episode_ratings_matrix_hides_trakt_ratings_for_unreleased_episodes(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(
                    trakt_id=21,
                    season=1,
                    number=1,
                    title="Released",
                    trakt_rating=7.0,
                    trakt_votes=100,
                    first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                ),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(
                    trakt_id=22,
                    season=1,
                    number=2,
                    title="Future",
                    trakt_rating=9.5,
                    trakt_votes=999,
                    first_aired=datetime.now(tz=UTC) + timedelta(days=7),
                ),
            )
        trakt_matrix = service.load_show_matrix(138748, provider="trakt")
        self.assertEqual(trakt_matrix.rows[1].cells[0].display_value, "?")
        self.assertEqual(trakt_matrix.rows[1].cells[0].trakt_state, "unrated")
        self.assertEqual(trakt_matrix.rows[1].cells[0].tooltip, "Future\nS01 E02\nn/a votes")
        self.assertEqual(trakt_matrix.seasons[0].avg_display, "7.0")
        self.assertEqual(trakt_matrix.seasons[-1].avg_display, "7.0")

    def test_episode_ratings_matrix_ignores_zero_vote_trakt_ratings_in_cells_and_averages(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=135985, title_type="show", title="That Time I Got Reincarnated as a Slime"))
            self.episode_repo.upsert_episode(
                session,
                135985,
                EpisodeSummary(
                    trakt_id=401,
                    season=4,
                    number=1,
                    title="Rated",
                    trakt_rating=7.6,
                    trakt_votes=120,
                    first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                ),
            )
            self.episode_repo.upsert_episode(
                session,
                135985,
                EpisodeSummary(
                    trakt_id=402,
                    season=4,
                    number=2,
                    title="Zero votes",
                    trakt_rating=0.0,
                    trakt_votes=0,
                    first_aired=datetime.now(tz=UTC) - timedelta(days=1),
                ),
            )
        trakt_matrix = service.load_show_matrix(135985, provider="trakt")
        self.assertEqual(trakt_matrix.rows[0].cells[0].display_value, "7.6")
        self.assertEqual(trakt_matrix.rows[1].cells[0].display_value, "?")
        self.assertEqual(trakt_matrix.rows[1].cells[0].trakt_state, "unrated")
        self.assertEqual(trakt_matrix.seasons[0].avg_display, "7.6")
        self.assertEqual(trakt_matrix.seasons[-1].avg_display, "7.6")

    def test_episode_ratings_matrix_uses_cached_rows_without_network_fetch(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=138748, title_type="show", title="The Capture"),
            )
            self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(trakt_id=101, season=0, number=1, title="Special", imdb_rating=8.1, imdb_votes=12),
            )
        self.trakt_client.get_show_episodes = lambda trakt_id: (_ for _ in ()).throw(RuntimeError("network must not be used"))
        matrix = service.load_show_matrix(138748)
        self.assertTrue(matrix.has_episodes)
        self.assertEqual([season.label for season in matrix.seasons], ["S0", "ALL"])


if __name__ == "__main__":
    unittest.main()
