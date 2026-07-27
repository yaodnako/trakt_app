from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from trakt_tracker.application.catalog import CatalogService
from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
)
from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.episode_imdb_resolver import EpisodeIMDbResolver
from trakt_tracker.application.episode_ratings_matrix import EpisodeRatingsMatrixService, rating_bucket_color
from trakt_tracker.application.enrich_queue import EnrichQueueService
from trakt_tracker.application.history import HistoryService
from trakt_tracker.application.history_read_model import HistoryReadModelService
from trakt_tracker.application.history_sync import HistorySyncWorkflow, _HistoryReconciliationScope
from trakt_tracker.application.interactions import InteractionService
from trakt_tracker.application.metadata_refresh_policy import TRIGGER_PAGE_CONTEXT, TRIGGER_VIEWPORT
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.search_watch import SearchWatchService
import trakt_tracker.application.episode_metadata as episode_metadata_module
import trakt_tracker.application.history_sync as history_sync_module
import trakt_tracker.application.services as services_module
from trakt_tracker.application.services import NotificationService, SyncService, build_services
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.domain import EpisodeSummary, ExploreResultPage, HistoryItemInput, ProgressSnapshot, RatingInput, TitleSummary
from trakt_tracker.infrastructure.trakt.client import TraktClient
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import EpisodeRepository, HistoryRepository, ProgressRepository, SyncStateRepository, TitleRepository, UserStateRepository


class _FakeConfig:
    tmdb_api_key = ""
    tmdb_read_access_token = ""


class NotificationActivityTests(unittest.TestCase):
    def test_notification_activity_records_only_known_sources_once_per_delivery(self) -> None:
        service = NotificationService(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )

        seq = service.record_activity(
            [
                {"source": "progress"},
                {"source": "progress"},
                {"source": "release"},
                {"source": "unknown"},
            ]
        )

        self.assertEqual(seq, 1)
        self.assertEqual(
            service.activity_after(0),
            [{"seq": 1, "sources": ["progress", "release"]}],
        )
        self.assertEqual(service.activity_after(1), [])
        self.assertEqual(service.record_activity([{"source": "unknown"}]), 1)
        self.assertEqual(service.current_activity_seq(), 1)

        service._workflow = SimpleNamespace(has_due_unseen_current_episode=lambda: True)
        service._release_tracking = SimpleNamespace(has_due_unacknowledged_release=lambda: False)
        self.assertEqual(service.refresh_pending_sources(), ["progress"])
        self.assertEqual(service.pending_sources(), ["progress"])


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
        self.history_item_batches: list[list[HistoryItemInput]] = []
        self.removed_history_items: list[HistoryItemInput] = []
        self.removed_history_batches: list[list[HistoryItemInput]] = []
        self.ratings: list[RatingInput] = []
        self.episode_details_calls: list[tuple[int, int, int]] = []
        self.title_details_calls: list[tuple[int, str]] = []
        self.watchlist_reads = 0
        self.watchlist_writes: list[tuple[str, int, bool]] = []
        self.watchlist_items = [
            TitleSummary(trakt_id=12, title_type="movie", title="Movie Watchlist"),
            TitleSummary(trakt_id=13, title_type="show", title="Show Watchlist"),
        ]
        self.title_details = TitleSummary(
            trakt_id=11,
            title_type="movie",
            title="Dune",
            overview="Spice.",
            poster_url="//poster.example/dune.jpg",
        )
        self.show_progress = ProgressSnapshot(
            trakt_id=138748,
            title="",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(trakt_id=301, season=1, number=2, title="Second"),
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

    def get_show_progress(self, trakt_id: int, use_cache: bool = True) -> ProgressSnapshot:
        return replace(self.show_progress, trakt_id=trakt_id)

    def add_history_item(self, item: HistoryItemInput) -> None:
        self.history_items.append(item)

    def add_history_items(self, items: list[HistoryItemInput]) -> None:
        self.history_item_batches.append(list(items))
        self.history_items.extend(items)

    def get_watchlist(self) -> list[TitleSummary]:
        self.watchlist_reads += 1
        return list(self.watchlist_items)

    def set_watchlist(self, title_type: str, trakt_id: int, *, watchlisted: bool) -> None:
        self.watchlist_writes.append((title_type, trakt_id, watchlisted))

    def remove_history_items(self, items: list[HistoryItemInput]) -> None:
        self.removed_history_batches.append(list(items))
        self.removed_history_items.extend(items)

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
    def __init__(self) -> None:
        self.season_still_calls: list[tuple[int, int]] = []

    def is_configured(self) -> bool:
        return True

    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        return replace(title, poster_url="https://tmdb.example/poster.jpg", status="released")

    def get_episode_still_url(self, show_tmdb_id: int, season: int, episode: int) -> str:
        return "https://tmdb.example/still.jpg"

    def get_season_episode_still_urls(self, show_tmdb_id: int, season: int) -> dict[int, str]:
        self.season_still_calls.append((show_tmdb_id, season))
        return {episode: "https://tmdb.example/still.jpg" for episode in range(1, 1000)}


class _FakeImdbClient:
    def __init__(self, *, ready: bool = False) -> None:
        self.ready = ready
        self.revision = "imdb-revision-1"
        self.revision_calls = 0
        self.episode_ids: dict[tuple[str, int, int], str] = {}
        self.episode_title_ids: dict[tuple[str, str], str] = {}
        self.episode_metadata: dict[str, dict] = {}
        self.episode_lookup_calls: list[tuple[str, int, int]] = []

    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        return replace(title, imdb_rating=8.5, imdb_votes=12345)

    def is_ready(self) -> bool:
        return self.ready

    def dataset_revision(self) -> str:
        self.revision_calls += 1
        return self.revision if self.ready else ""

    def enrich_episode(self, episode: EpisodeSummary) -> EpisodeSummary:
        return episode

    def lookup_episode_imdb_id(self, show_imdb_id: str, season_number: int, episode_number: int) -> str:
        self.episode_lookup_calls.append((show_imdb_id, season_number, episode_number))
        return self.episode_ids.get((show_imdb_id, season_number, episode_number), "")

    def lookup_overflow_episode_imdb_id(self, show_imdb_id: str, season_number: int, episode_number: int) -> str:
        if season_number != 1:
            return ""
        season_numbers = sorted(
            {
                season
                for parent, season, _episode in self.episode_ids
                if parent == show_imdb_id and season > 0
            }
        )
        if len(season_numbers) < 2:
            return ""
        remaining = episode_number
        for season in season_numbers:
            max_episode = max(
                episode
                for parent, item_season, episode in self.episode_ids
                if parent == show_imdb_id and item_season == season
            )
            if remaining <= max_episode:
                if season == season_number:
                    return ""
                return self.episode_ids.get((show_imdb_id, season, remaining), "")
            remaining -= max_episode
        return ""

    def lookup_episode_imdb_id_by_title(self, show_imdb_id: str, episode_title: str) -> str:
        normalized_title = " ".join((episode_title or "").strip().casefold().split())
        return self.episode_title_ids.get((show_imdb_id, normalized_title), "")

    def lookup_episode_metadata(self, imdb_id: str) -> dict | None:
        return self.episode_metadata.get(imdb_id)


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


class _FakeImageCache:
    def __init__(self, cached_urls: set[str] | None = None) -> None:
        self.cached_urls = cached_urls or set()

    def get_any_bytes(self, key: str) -> bytes | None:
        return b"cached" if key in self.cached_urls else None


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

    def _imdb_episode_metadata(
        self,
        *,
        parent: str = "tt-show",
        season: int = 1,
        episode: int = 1,
        title: str = "Pilot",
        rating: float | None = 8.0,
        votes: int | None = 100,
    ) -> dict:
        return {
            "parent_imdb_id": parent,
            "season": season,
            "episode": episode,
            "title": title,
            "imdb_rating": rating,
            "imdb_votes": votes,
        }

    def test_episode_imdb_resolver_uses_matching_trakt_id(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_metadata["tt-trakt"] = self._imdb_episode_metadata(title="King of Hell", rating=7.8, votes=15500)
        self.imdb.episode_ids[("tt-show", 1, 1)] = "tt-trakt"

        resolution = EpisodeIMDbResolver(self.imdb).resolve(
            show_imdb_id="tt-show",
            season=1,
            episode=1,
            title="King of Hell",
            trakt_imdb_id="tt-trakt",
        )

        self.assertEqual(resolution.imdb_id, "tt-trakt")
        self.assertEqual(resolution.imdb_rating, 7.8)
        self.assertEqual(resolution.imdb_votes, 15500)

    def test_episode_imdb_resolver_uses_number_candidate_when_title_matches(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_metadata["tt-stale"] = self._imdb_episode_metadata(season=1, episode=2, title="Other", rating=9.1, votes=200)
        self.imdb.episode_metadata["tt-number"] = self._imdb_episode_metadata(title="King of Hell", rating=7.8, votes=15500)
        self.imdb.episode_ids[("tt-show", 1, 1)] = "tt-number"

        resolution = EpisodeIMDbResolver(self.imdb).resolve(
            show_imdb_id="tt-show",
            season=1,
            episode=1,
            title="King of Hell",
            trakt_imdb_id="tt-stale",
        )

        self.assertEqual(resolution.imdb_id, "tt-number")
        self.assertEqual(resolution.imdb_rating, 7.8)

    def test_episode_imdb_resolver_rejects_rating_for_other_episode(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_metadata["tt-stale"] = self._imdb_episode_metadata(season=1, episode=2, title="Other", rating=9.1, votes=200)

        resolution = EpisodeIMDbResolver(self.imdb).resolve(
            show_imdb_id="tt-show",
            season=1,
            episode=1,
            title="King of Hell",
            trakt_imdb_id="tt-stale",
        )

        self.assertEqual(resolution.imdb_id, "")
        self.assertIsNone(resolution.imdb_rating)

    def test_episode_imdb_resolver_suppresses_rating_for_generic_conflict(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_metadata["tt-stale"] = self._imdb_episode_metadata(season=1, episode=1, title="Named Episode", rating=9.1, votes=200)
        self.imdb.episode_metadata["tt-number"] = self._imdb_episode_metadata(season=1, episode=2, title="Different Named Episode", rating=7.8, votes=15500)
        self.imdb.episode_ids[("tt-show", 1, 2)] = "tt-number"

        resolution = EpisodeIMDbResolver(self.imdb).resolve(
            show_imdb_id="tt-show",
            season=1,
            episode=2,
            title="Episode 2",
            trakt_imdb_id="tt-stale",
        )

        self.assertEqual(resolution.imdb_id, "tt-number")
        self.assertIsNone(resolution.imdb_rating)
        self.assertIsNone(resolution.imdb_votes)

    def test_episode_imdb_resolver_uses_overflow_number_when_imdb_splits_seasons(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_ids[("tt-show", 1, 28)] = "tt-season-one-finale"
        self.imdb.episode_ids[("tt-show", 2, 1)] = "tt-season-two-premiere"
        self.imdb.episode_metadata["tt-season-two-premiere"] = self._imdb_episode_metadata(
            season=2,
            episode=1,
            title="Ja iko ka",
            rating=8.8,
            votes=4820,
        )

        resolution = EpisodeIMDbResolver(self.imdb).resolve(
            show_imdb_id="tt-show",
            season=1,
            episode=29,
            title="Shall We Go, Then?",
        )

        self.assertEqual(resolution.imdb_id, "tt-season-two-premiere")
        self.assertEqual(resolution.imdb_rating, 8.8)
        self.assertEqual(resolution.imdb_votes, 4820)

    def test_episode_imdb_resolver_prefers_exact_number_before_overflow_number(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_ids[("tt-show", 1, 29)] = "tt-exact"
        self.imdb.episode_ids[("tt-show", 2, 1)] = "tt-overflow"
        self.imdb.episode_metadata["tt-exact"] = self._imdb_episode_metadata(
            season=1,
            episode=29,
            title="Exact Episode",
            rating=7.1,
            votes=120,
        )
        self.imdb.episode_metadata["tt-overflow"] = self._imdb_episode_metadata(
            season=2,
            episode=1,
            title="Overflow Episode",
            rating=8.8,
            votes=4820,
        )

        resolution = EpisodeIMDbResolver(self.imdb).resolve(
            show_imdb_id="tt-show",
            season=1,
            episode=29,
            title="Exact Episode",
        )

        self.assertEqual(resolution.imdb_id, "tt-exact")
        self.assertEqual(resolution.imdb_rating, 7.1)

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

    def test_catalog_watchlist_snapshot_is_local_and_updates_after_portal_action(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )

        self.assertFalse(service.has_watchlist_snapshot())
        service.refresh_watchlist_snapshot()

        self.assertTrue(service.has_watchlist_snapshot())
        self.assertEqual(self.trakt_client.watchlist_reads, 1)
        self.assertEqual(service.watchlist_keys(title_type="show"), {("show", 13)})
        self.assertEqual(service.watchlist_keys(), {("movie", 12), ("show", 13)})
        self.assertEqual(
            {(item.title_type, item.trakt_id) for item in service.local_watchlist_titles()},
            {("movie", 12), ("show", 13)},
        )
        self.assertEqual(self.trakt_client.watchlist_reads, 1)

        service.set_watchlisted("show", 13, watchlisted=False)
        service.set_watchlisted("movie", 14, watchlisted=True)

        self.assertEqual(self.trakt_client.watchlist_writes, [("show", 13, False), ("movie", 14, True)])
        self.assertEqual(service.watchlist_keys(), {("movie", 12), ("movie", 14)})
        self.assertEqual(self.trakt_client.watchlist_reads, 1)

    def test_catalog_service_search_enriches_imdb_ratings_before_saving_state(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            _FakeImdbClient(ready=True),
        )
        self.trakt_client.search_titles = lambda query, title_type=None: [
            TitleSummary(
                trakt_id=11,
                title_type="movie",
                title="Dune",
                poster_url="//poster.example/dune.jpg",
                imdb_id="tt1160419",
            )
        ]

        results = service.search_titles("Dune")

        self.assertEqual(results[0].imdb_rating, 8.5)
        self.assertEqual(results[0].imdb_votes, 12345)
        self.assertEqual(results[0].ratings_status, ENRICH_STATUS_READY)
        state = service.load_last_search_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["results"][0].imdb_rating, 8.5)
        with self.db.session() as session:
            row = self.titles.get_title(session, 11)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.imdb_rating, 8.5)
            self.assertEqual(row.ratings_status, ENRICH_STATUS_READY)

    def test_catalog_service_saved_search_merges_cached_title_metadata(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )

        service.save_last_search_state(
            "Dune",
            None,
            [
                TitleSummary(
                    trakt_id=11,
                    title_type="movie",
                    title="Dune",
                    poster_url="//poster.example/dune.jpg",
                    imdb_id="tt1160419",
                )
            ],
            imdb_min="8",
            trakt_min="7.5",
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=11,
                    title_type="movie",
                    title="Dune",
                    poster_url="https://poster.example/dune.jpg",
                    imdb_id="tt1160419",
                    imdb_rating=8.5,
                    imdb_votes=12345,
                ),
            )

        state = service.load_last_search_state()

        self.assertIsNotNone(state)
        self.assertEqual(state["results"][0].poster_url, "https://poster.example/dune.jpg")
        self.assertEqual(state["results"][0].imdb_rating, 8.5)
        self.assertEqual(state["results"][0].imdb_votes, 12345)
        self.assertEqual(state["imdb_min"], "8")
        self.assertEqual(state["trakt_min"], "7.5")

    def test_catalog_service_saved_search_serializes_refreshed_datetimes(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        refreshed_at = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
        released_at = datetime(2027, 1, 1, tzinfo=UTC)

        service.save_last_search_state(
            "Dune",
            None,
            [
                TitleSummary(
                    trakt_id=11,
                    title_type="movie",
                    title="Dune",
                    ratings_status=ENRICH_STATUS_READY,
                    ratings_refreshed_at=refreshed_at,
                    released_at=released_at,
                )
            ],
        )
        state = service.load_last_search_state()

        self.assertIsNotNone(state)
        self.assertEqual(state["results"][0].ratings_status, ENRICH_STATUS_READY)
        self.assertEqual(state["results"][0].ratings_refreshed_at, refreshed_at)
        self.assertEqual(state["results"][0].released_at, released_at)

    def test_catalog_service_search_enrichment_persists_ratings_status(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            _FakeImdbClient(ready=True),
        )

        title = service.enrich_title_with_tmdb(
            TitleSummary(
                trakt_id=11,
                title_type="movie",
                title="Dune",
                poster_url="//poster.example/dune.jpg",
                imdb_id="tt1160419",
            )
        )

        self.assertEqual(title.imdb_rating, 8.5)
        self.assertEqual(title.ratings_status, ENRICH_STATUS_READY)
        with self.db.session() as session:
            row = self.titles.get_title(session, 11)
            self.assertIsNotNone(row)
            self.assertEqual(row.ratings_status, ENRICH_STATUS_READY)

    def test_trakt_history_payload_rejects_undated_items(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        client._request = lambda *_args, **_kwargs: {}

        with self.assertRaises(ValueError):
            client.add_history_item(
                HistoryItemInput(
                    title_type="movie",
                    trakt_id=77,
                    watched_at=None,
                    title="Arrival",
                )
            )

    def test_trakt_history_remove_uses_same_media_payload_for_undated_replace(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        calls = []
        client._request = lambda *_args, **kwargs: calls.append(kwargs) or {}

        client.remove_history_items(
            [
                HistoryItemInput(
                    title_type="show",
                    trakt_id=501,
                    watched_at=None,
                    season=1,
                    episode=1,
                    title="Pilot",
                )
            ]
        )

        self.assertEqual(calls[0]["json"], {"episodes": [{"ids": {"trakt": 501}}]})

    def test_trakt_history_page_exposes_authoritative_pagination_headers(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ([{"id": 10, "type": "episode"}], {"x-pagination-page-count": "3"})

        client._request = request

        items, headers = client.get_watch_history_page(limit=1000, page=2)

        self.assertEqual(items[0]["id"], 10)
        self.assertEqual(headers["x-pagination-page-count"], "3")
        self.assertEqual(calls[0][0:2], ("GET", "/sync/history"))
        self.assertEqual(calls[0][2]["params"]["limit"], 1000)
        self.assertFalse(calls[0][2]["use_cache"])
        self.assertTrue(calls[0][2]["include_headers"])

    def test_trakt_show_progress_payload_without_title_stays_untitled(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        client._request = lambda *_args, **_kwargs: {
            "completed": 78,
            "aired": 79,
            "next_episode": {"season": 4, "number": 7, "title": "The Mastermind's Identity", "ids": {"trakt": 700}},
        }

        progress = client.get_show_progress(135985)

        self.assertEqual(progress.title, "")

    def test_trakt_watchlist_reads_movies_and_shows(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        paths = []

        def request(_method, path, **_kwargs):
            paths.append(path)
            title_type = "movie" if path.endswith("/movies") else "show"
            return [
                {
                    "type": title_type,
                    "listed_at": "2026-07-09T12:30:00.000Z",
                    title_type: {
                        "title": "Saved title",
                        "year": 2026,
                        ("released" if title_type == "movie" else "first_aired"): "2026-06-01T00:00:00.000Z",
                        "ids": {"trakt": 41, "slug": "saved-title"},
                    },
                }
            ]

        client._request = request
        titles = client.get_watchlist()

        self.assertEqual(paths, ["/sync/watchlist/movies", "/sync/watchlist/shows"])
        self.assertEqual([title.title_type for title in titles], ["movie", "show"])
        self.assertTrue(all(title.is_watchlisted for title in titles))
        self.assertTrue(all(title.watchlisted_at is not None for title in titles))
        self.assertTrue(all(title.released_at is not None for title in titles))

    def test_trakt_watchlist_remove_uses_sync_remove_payload(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        calls = []
        client._request = lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {}

        client.set_watchlist("show", 41, watchlisted=False)

        self.assertEqual(calls[0][0:2], ("POST", "/sync/watchlist/remove"))
        self.assertEqual(calls[0][2]["json"], {"shows": [{"ids": {"trakt": 41}}]})

    def test_trakt_explore_normalizes_feeds_and_pagination(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        calls = []

        def request(_method, path, **kwargs):
            calls.append((path, kwargs))
            title_type = "movie" if path.startswith("/movies/") else "show"
            feed = path.rsplit("/", 1)[-1]
            title_payload = {
                "title": f"{feed.title()} title",
                "year": 2027,
                "first_aired": "2027-01-10T00:00:00.000Z",
                "released": "2027-01-10",
                "ids": {"trakt": 91, "slug": f"{feed}-title"},
            }
            if feed == "popular":
                payload = [title_payload]
            else:
                payload = [
                    {
                        title_type: title_payload,
                        ("watchers" if feed == "trending" else "list_count"): 1234,
                    }
                ]
            return payload, {"x-pagination-page": "2", "x-pagination-page-count": "5"}

        client._request = request

        anticipated = client.get_explore_titles("show", "anticipated", page=2, limit=24)
        trending = client.get_explore_titles("movie", "trending", page=2, limit=24, trakt_min=7.5)
        popular = client.get_explore_titles("show", "popular", page=2, limit=24)

        self.assertEqual((anticipated.page, anticipated.page_count), (2, 5))
        self.assertEqual((anticipated.items[0].explore_metric_kind, anticipated.items[0].explore_metric_count), ("lists", 1234))
        self.assertFalse(anticipated.items[0].catalog_actions_available)
        self.assertEqual((trending.items[0].explore_metric_kind, trending.items[0].explore_metric_count), ("watching", 1234))
        self.assertTrue(trending.items[0].catalog_actions_available)
        self.assertEqual(popular.items[0].explore_metric_kind, "")
        self.assertIsNone(popular.items[0].explore_metric_count)
        self.assertEqual(
            [path for path, _kwargs in calls],
            ["/shows/anticipated", "/movies/trending", "/shows/popular"],
        )
        self.assertTrue(all(call[1]["use_cache"] is False for call in calls))
        self.assertTrue(all(call[1]["include_headers"] is True for call in calls))
        self.assertEqual(calls[1][1]["params"]["ratings"], "75-100")
        self.assertNotIn("ratings", calls[0][1]["params"])

    def test_trakt_search_page_reads_pagination_without_unsupported_rating_filter(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://localhost")
        calls = []

        def request(_method, path, **kwargs):
            calls.append((path, kwargs))
            return [
                {
                    "type": "movie",
                    "movie": {
                        "title": "Dune",
                        "rating": 8.2,
                        "released": "2027-01-01",
                        "ids": {"trakt": 11, "slug": "dune-2021"},
                    },
                }
            ], {"x-pagination-page": "2", "x-pagination-page-count": "4"}

        client._request = request
        result = client.get_search_titles_page("Dune", None, page=2, limit=24)

        self.assertEqual((result.page, result.page_count), (2, 4))
        self.assertEqual(result.items[0].title, "Dune")
        self.assertEqual(result.items[0].released_at, datetime(2027, 1, 1))
        self.assertEqual(calls[0][0], "/search/movie,show")
        self.assertNotIn("ratings", calls[0][1]["params"])
        self.assertFalse(calls[0][1]["use_cache"])

    def test_catalog_search_imdb_filter_fills_page_from_additional_provider_pages(self) -> None:
        calls = []

        class SearchClient:
            def get_search_titles_page(self, query, title_type, *, page, limit):
                calls.append(page)
                items = [
                    TitleSummary(
                        trakt_id=page * 100 + index,
                        title_type="movie",
                        title=f"{query} {page}-{index}",
                        imdb_rating=8.5 if index % 2 == 0 else 7.0,
                        trakt_rating=8.0,
                    )
                    for index in range(limit)
                ]
                return ExploreResultPage(items=items, page=page, page_count=3)

        service = CatalogService(
            self.db,
            _FakeAuthService(SearchClient()),
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        result = service.filtered_search_titles(
            "Dune",
            None,
            page=1,
            limit=4,
            imdb_min=8.0,
            trakt_min=7.5,
            max_scan_pages=3,
            excluded_keys={("movie", 100)},
        )

        self.assertEqual(len(result.items), 4)
        self.assertNotIn(100, {item.trakt_id for item in result.items})
        self.assertEqual(result.page_count, 2)
        self.assertEqual(calls, [1, 2, 3])

    def test_catalog_search_rating_filters_persist_in_sync_state(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        service.save_search_rating_filters("8", "7", hide_watchlisted=True, hide_history=True)
        self.assertEqual(
            service.load_search_rating_filters(),
            {"imdb_min": "8", "trakt_min": "7", "hide_watchlisted": True, "hide_history": True},
        )

    def test_catalog_explore_imdb_filter_fills_pages_and_reuses_short_cache(self) -> None:
        calls = []

        class ExploreClient:
            def get_explore_titles(self, title_type, feed, *, page, limit, trakt_min=None):
                calls.append((page, trakt_min))
                items = [
                    TitleSummary(
                        trakt_id=page * 100 + index,
                        title_type=title_type,
                        title=f"Title {page}-{index}",
                        imdb_rating=8.5 if index % 2 == 0 else 7.0,
                        trakt_rating=8.0,
                    )
                    for index in range(limit)
                ]
                return ExploreResultPage(items=items, page=page, page_count=3)

        service = CatalogService(
            self.db,
            _FakeAuthService(ExploreClient()),
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        first = service.filtered_explore_titles(
            "show",
            "trending",
            page=1,
            limit=4,
            imdb_min=8.0,
            trakt_min=7.5,
            max_scan_pages=3,
        )
        second = service.filtered_explore_titles(
            "show",
            "trending",
            page=2,
            limit=4,
            imdb_min=8.0,
            trakt_min=7.5,
            max_scan_pages=3,
        )

        self.assertEqual(len(first.items), 4)
        self.assertEqual(first.page_count, 2)
        self.assertEqual(len(second.items), 2)
        self.assertEqual(second.page_count, 2)
        self.assertEqual(calls, [(1, 7.5), (2, 7.5), (3, 7.5)])

        refreshed = service.refresh_explore_titles(
            "show",
            "trending",
            page=1,
            limit=4,
            imdb_min=8.0,
            trakt_min=7.5,
            max_scan_pages=3,
        )
        local = service.local_explore_titles(
            "show",
            "trending",
            page=1,
            limit=4,
            imdb_min=8.0,
            trakt_min=7.5,
            max_scan_pages=3,
        )
        self.assertIsNotNone(local)
        self.assertEqual([item.trakt_id for item in local.items], [item.trakt_id for item in refreshed.items])
        self.assertEqual(calls, [(1, 7.5), (2, 7.5), (3, 7.5)])

    def test_catalog_explore_rating_filters_persist_in_sync_state(self) -> None:
        service = CatalogService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
        )
        service.save_explore_rating_filters("8.2", "7.5", hide_watchlisted=True, hide_history=True)
        self.assertEqual(
            service.load_explore_rating_filters(),
            {
                "imdb_min": "8.2",
                "trakt_min": "7.5",
                "hide_watchlisted": True,
                "hide_history": True,
                "hide_releases": False,
            },
        )

    def test_history_sync_refresh_show_uses_cached_title_when_progress_payload_has_no_title(self) -> None:
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=135985,
                    title_type="show",
                    title="That Time I Got Reincarnated as a Slime",
                ),
            )

        workflow.refresh_show(135985)

        with self.db.session() as session:
            rows = ProgressRepository().list_in_progress(session)
        self.assertEqual(rows[0].title, "That Time I Got Reincarnated as a Slime")

    def test_progress_sync_does_not_persist_show_id_fallback_when_title_lookup_fails(self) -> None:
        def fail_title_details(trakt_id: int, title_type: str, use_cache: bool = True) -> TitleSummary:
            raise RuntimeError("title lookup failed")

        self.trakt_client.get_title_details = fail_title_details
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

        progress = workflow.refresh_show_progress(135985)

        self.assertEqual(progress.title, "")
        with self.db.session() as session:
            rows = ProgressRepository().list_in_progress(session)
        self.assertEqual(rows[0].title, "")

    def test_progress_sync_removes_zero_completed_snapshot(self) -> None:
        progress_repo = ProgressRepository()
        with self.db.session() as session:
            progress_repo.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=5,
                    title="Example",
                    completed=1,
                    aired=12,
                    percent_completed=8.3,
                    next_episode=EpisodeSummary(trakt_id=501, season=1, number=2, title="Two"),
                ),
            )
        self.trakt_client.show_progress = ProgressSnapshot(
            trakt_id=5,
            title="",
            completed=0,
            aired=12,
            percent_completed=0.0,
            next_episode=EpisodeSummary(trakt_id=500, season=1, number=1, title="One"),
        )
        workflow = ProgressSyncWorkflow(
            self.db,
            self.auth,
            progress_repo,
            self.episode_repo,
            self.titles,
            self.user_states,
            self.sync_state,
            lambda _config: _FakeTmdbClient(),
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )

        workflow.refresh_show_progress(5, fresh=True)

        with self.db.session() as session:
            self.assertEqual(progress_repo.list_sync_show_ids(session), [])

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

    def test_history_service_allows_undated_watch_and_sorts_it_last(self) -> None:
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
        service.add_history_item(
            HistoryItemInput(
                title_type="movie",
                trakt_id=77,
                watched_at=None,
                title="Arrival",
            )
        )
        dated_at = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        service.add_history_item(
            HistoryItemInput(
                title_type="movie",
                trakt_id=78,
                watched_at=dated_at,
                title="Dune",
            )
        )

        self.assertEqual(len(self.trakt_client.history_items), 1)
        self.assertEqual(self.trakt_client.removed_history_items, [])
        self.assertEqual(self.trakt_client.history_items[0].watched_at, dated_at)
        rows = service.history(title_type="movie")
        self.assertEqual([row["title"] for row in rows], ["Dune", "Arrival"])
        self.assertTrue(rows[0]["watched_at_known"])
        self.assertFalse(rows[1]["watched_at_known"])

    def test_search_watch_whole_show_excludes_specials_and_future_episodes(self) -> None:
        history_service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )
        service = SearchWatchService(
            self.db,
            self.auth,
            self.titles,
            self.history_repo,
            self.episode_repo,
            history_service,
        )
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(
                        trakt_id=500,
                        season=0,
                        number=1,
                        title="Special",
                        first_aired=datetime.now(tz=UTC) - timedelta(days=10),
                    ),
                    EpisodeSummary(
                        trakt_id=501,
                        season=1,
                        number=1,
                        title="Pilot",
                        first_aired=datetime.now(tz=UTC) - timedelta(days=5),
                    ),
                    EpisodeSummary(
                        trakt_id=502,
                        season=1,
                        number=2,
                        title="Future",
                        first_aired=datetime.now(tz=UTC) + timedelta(days=5),
                    ),
                ],
            )
        count = service.mark_watch(
            title_type="show",
            trakt_id=5,
            title="Example",
            scope="title",
            watched_at=None,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self.trakt_client.removed_history_items, [])
        self.assertEqual(self.trakt_client.history_items, [])
        rows = history_service.history(title_type="show")
        self.assertEqual([(row["season"], row["episode"], row["watched_at_known"]) for row in rows], [(1, 1, False)])

    def test_search_watch_panel_does_not_synchronously_enrich_episode_stills(self) -> None:
        class _BlockingEpisodeMetadata:
            def enrich_episode_stills(self, *args, **kwargs):
                raise AssertionError("watch panel must not wait for still enrichment")

        history_service = HistoryService(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            self.episode_repo,
            self.history_read_model,
            self.episode_metadata,
        )
        service = SearchWatchService(
            self.db,
            self.auth,
            self.titles,
            self.history_repo,
            self.episode_repo,
            history_service,
            episode_metadata=_BlockingEpisodeMetadata(),
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=5,
                    title_type="show",
                    title="Example",
                    trakt_rating=8.2,
                    trakt_votes=1200,
                    imdb_rating=8.4,
                    imdb_votes=3400,
                    ratings_status="ready",
                ),
            )
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(
                        trakt_id=501,
                        season=1,
                        number=1,
                        title="Pilot",
                        first_aired=datetime.now(tz=UTC) - timedelta(days=5),
                    ),
                ],
            )
            episode_row = self.episode_repo.find_episode(session, 5, 1, 1)
            episode_row.imdb_season = 2
            episode_row.imdb_episode = 1

        panel = service.load_show_panel(5)

        self.assertEqual(panel.title, "Example")
        self.assertEqual(panel.title_trakt_rating, 8.2)
        self.assertEqual(panel.title_trakt_votes, 1200)
        self.assertEqual(panel.title_imdb_rating, 8.4)
        self.assertEqual(panel.title_imdb_votes, 3400)
        self.assertEqual(panel.seasons[0].episodes[0].title, "Pilot")
        self.assertEqual(panel.seasons[0].episodes[0].still_url, "")
        self.assertEqual(panel.seasons[0].episodes[0].imdb_season, 2)
        self.assertEqual(panel.seasons[0].episodes[0].imdb_episode, 1)

    def test_search_watch_panel_opens_first_released_unwatched_regular_episode(self) -> None:
        history_service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        service = SearchWatchService(
            self.db, self.auth, self.titles, self.history_repo, self.episode_repo,
            history_service, episode_metadata=self.episode_metadata,
        )
        now = datetime.now(tz=UTC)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(trakt_id=500, season=0, number=1, title="Special", first_aired=now - timedelta(days=1)),
                    EpisodeSummary(trakt_id=501, season=1, number=1, title="Seen", first_aired=now - timedelta(days=2)),
                    EpisodeSummary(trakt_id=502, season=1, number=2, title="Future", first_aired=now + timedelta(days=2)),
                    EpisodeSummary(trakt_id=503, season=2, number=1, title="Next", first_aired=now - timedelta(days=1)),
                ],
            )
            self.history_repo.add_event(
                session,
                trakt_history_id=1,
                title_trakt_id=5,
                title="Example",
                title_type="show",
                action="watched",
                watched_at=now,
                season=1,
                episode=1,
                source="trakt",
            )

        panel = service.load_show_panel(5)

        self.assertEqual(panel.default_episode_key, (2, 1))
        self.assertEqual(panel.watched_frontier_key, (1, 1))
        self.assertTrue(next(season for season in panel.seasons if season.season == 2).is_default)

    def test_search_watch_panel_includes_episode_user_rating(self) -> None:
        history_service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        service = SearchWatchService(
            self.db, self.auth, self.titles, self.history_repo, self.episode_repo,
            history_service, episode_metadata=self.episode_metadata,
        )
        now = datetime.now(tz=UTC)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [EpisodeSummary(trakt_id=501, season=1, number=1, title="Pilot", first_aired=now)],
            )
            self.history_repo.add_event(
                session, trakt_history_id=1, title_trakt_id=5, title="Example", title_type="show",
                action="watched", watched_at=now, season=1, episode=1, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=2, title_trakt_id=5, title="Example", title_type="show",
                action="rated", watched_at=now, season=1, episode=1, rating=9, source="trakt",
            )

        panel = service.load_show_panel(5)

        self.assertEqual(panel.seasons[0].episodes[0].user_rating, 9)

    def test_search_watch_imdb_layout_splits_trakt_season_and_marks_one_batch(self) -> None:
        history_service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        service = SearchWatchService(
            self.db, self.auth, self.titles, self.history_repo, self.episode_repo,
            history_service, episode_metadata=self.episode_metadata,
        )
        now = datetime.now(tz=UTC)
        episodes = [
            EpisodeSummary(trakt_id=500, season=0, number=1, title="Special", first_aired=now - timedelta(days=5)),
            EpisodeSummary(trakt_id=501, season=1, number=1, title="IMDb S1E2", first_aired=now - timedelta(days=5)),
            EpisodeSummary(trakt_id=502, season=1, number=2, title="IMDb S1E1", first_aired=now - timedelta(days=4)),
            EpisodeSummary(trakt_id=503, season=1, number=3, title="IMDb S2E2", first_aired=now - timedelta(days=3)),
            EpisodeSummary(trakt_id=504, season=1, number=4, title="IMDb S2E1", first_aired=now - timedelta(days=2)),
            EpisodeSummary(trakt_id=505, season=1, number=5, title="Future", first_aired=now + timedelta(days=5)),
        ]
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Split Show"))
            self.episode_repo.replace_show_episodes(session, 5, episodes)
            for number, imdb_season, imdb_episode in (
                (1, 1, 2),
                (2, 1, 1),
                (3, 2, 2),
                (4, 2, 1),
            ):
                row = self.episode_repo.find_episode(session, 5, 1, number)
                row.imdb_season = imdb_season
                row.imdb_episode = imdb_episode
            for history_id, episode in enumerate((1, 2, 3), start=1):
                self.history_repo.add_event(
                    session,
                    trakt_history_id=history_id,
                    title_trakt_id=5,
                    title="Split Show",
                    title_type="show",
                    action="watched",
                    watched_at=now,
                    season=1,
                    episode=episode,
                    source="trakt",
                )

        panel = service.load_show_panel(5, season_layout="imdb")

        self.assertEqual([season.season for season in panel.seasons], [0, 1, 2])
        self.assertTrue(panel.imdb_mapping_complete)
        self.assertEqual(panel.default_episode_key, (1, 4))
        self.assertTrue(next(season for season in panel.seasons if season.season == 2).is_default)
        imdb_season_one = next(season for season in panel.seasons if season.season == 1)
        self.assertEqual(
            [(episode.season, episode.number, episode.imdb_episode) for episode in imdb_season_one.episodes],
            [(1, 2, 1), (1, 1, 2), (1, 5, None)],
        )
        self.assertEqual(imdb_season_one.action_released_count, 2)
        self.assertFalse(next(season for season in panel.seasons if season.season == 0).bulk_allowed)

        count = service.mark_watch(
            title_type="show",
            trakt_id=5,
            title="Split Show",
            scope="season",
            season=2,
            season_layout="imdb",
            watched_at=now,
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(self.trakt_client.history_item_batches), 1)
        self.assertEqual([item.trakt_id for item in self.trakt_client.history_item_batches[0]], [504])

    def test_search_watch_trakt_season_marks_only_unwatched_released_episodes(self) -> None:
        history_service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        service = SearchWatchService(
            self.db, self.auth, self.titles, self.history_repo, self.episode_repo,
            history_service,
        )
        now = datetime.now(tz=UTC)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(trakt_id=501, season=1, number=1, title="Seen", first_aired=now),
                    EpisodeSummary(trakt_id=502, season=1, number=2, title="New", first_aired=now),
                    EpisodeSummary(
                        trakt_id=503,
                        season=1,
                        number=3,
                        title="Future",
                        first_aired=now + timedelta(days=5),
                    ),
                ],
            )
            self.history_repo.add_event(
                session,
                trakt_history_id=1,
                title_trakt_id=5,
                title="Example",
                title_type="show",
                action="watched",
                watched_at=now,
                season=1,
                episode=1,
                source="trakt",
            )

        count = service.mark_watch(
            title_type="show",
            trakt_id=5,
            title="Example",
            scope="season",
            season=1,
            season_layout="trakt",
            watched_at=now,
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(self.trakt_client.history_item_batches), 1)
        self.assertEqual([item.trakt_id for item in self.trakt_client.history_item_batches[0]], [502])

    def test_search_watch_incomplete_imdb_mapping_blocks_bulk_but_not_episode(self) -> None:
        class _RepairMetadata:
            def __init__(self) -> None:
                self.repairs: list[int] = []

            def needs_episode_imdb_reconciliation(self, show_trakt_id: int) -> bool:
                return True

            def repair_episode_imdb_ratings(self, show_trakt_id: int) -> int:
                self.repairs.append(show_trakt_id)
                return 1

        metadata = _RepairMetadata()
        history_service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        service = SearchWatchService(
            self.db, self.auth, self.titles, self.history_repo, self.episode_repo,
            history_service, episode_metadata=metadata,
        )
        now = datetime.now(tz=UTC)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Incomplete"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(trakt_id=500, season=0, number=1, title="Special", first_aired=now),
                    EpisodeSummary(trakt_id=501, season=1, number=1, title="Mapped", first_aired=now),
                    EpisodeSummary(trakt_id=502, season=1, number=2, title="Unmapped", first_aired=now),
                    EpisodeSummary(trakt_id=601, season=2, number=1, title="Future", first_aired=now + timedelta(days=5)),
                ],
            )
            mapped = self.episode_repo.find_episode(session, 5, 1, 1)
            mapped.imdb_season = 1
            mapped.imdb_episode = 1

        panel = service.load_show_panel(5, season_layout="imdb")

        self.assertFalse(panel.imdb_mapping_complete)
        self.assertTrue(panel.imdb_mapping_pending)
        self.assertTrue(all(not season.bulk_allowed for season in panel.seasons))
        with self.assertRaisesRegex(RuntimeError, "mapping is incomplete"):
            service.mark_watch(
                title_type="show",
                trakt_id=5,
                title="Incomplete",
                scope="season",
                season=1,
                season_layout="imdb",
                watched_at=now,
            )
        self.assertEqual(self.trakt_client.history_item_batches, [])

        count = service.mark_watch(
            title_type="show",
            trakt_id=5,
            title="Incomplete",
            scope="episode",
            season=1,
            episode=2,
            season_layout="trakt",
            watched_at=now,
        )
        self.assertEqual(count, 1)
        self.assertEqual([item.trakt_id for item in self.trakt_client.history_item_batches[0]], [502])
        self.assertEqual(service.repair_imdb_seasons(5), 1)
        self.assertEqual(metadata.repairs, [5])

    def test_search_watch_imdb_unwatch_is_exact_reversible_and_transactional(self) -> None:
        history_service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        service = SearchWatchService(
            self.db, self.auth, self.titles, self.history_repo, self.episode_repo,
            history_service, episode_metadata=self.episode_metadata,
        )
        watched_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Split Show"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(trakt_id=501, season=1, number=1, title="IMDb One"),
                    EpisodeSummary(trakt_id=502, season=1, number=2, title="IMDb Two"),
                    EpisodeSummary(trakt_id=601, season=2, number=1, title="IMDb One Continued"),
                ],
            )
            for season, episode, imdb_season, imdb_episode in (
                (1, 1, 1, 1),
                (1, 2, 2, 1),
                (2, 1, 1, 2),
            ):
                row = self.episode_repo.find_episode(session, 5, season, episode)
                row.imdb_season = imdb_season
                row.imdb_episode = imdb_episode
                self.history_repo.add_event(
                    session,
                    trakt_history_id=season * 10 + episode,
                    title_trakt_id=5,
                    title="Split Show",
                    title_type="show",
                    action="watched",
                    watched_at=watched_at,
                    season=season,
                    episode=episode,
                    source="trakt",
                )

        restore = service.unmark_scope(
            title_type="show",
            trakt_id=5,
            scope="season",
            season=1,
            season_layout="imdb",
        )

        self.assertEqual(restore["season_layout"], "imdb")
        self.assertEqual(len(self.trakt_client.removed_history_batches), 1)
        self.assertEqual([item.trakt_id for item in self.trakt_client.removed_history_batches[0]], [501, 601])
        with self.db.session() as session:
            self.assertEqual(self.history_repo.watched_episode_keys(session, 5), {(1, 2)})

        items = list(restore["items"])
        for item in items:
            item["watched_at"] = datetime.fromisoformat(item["watched_at"])
        service.restore_scope(items=items)
        with self.db.session() as session:
            self.assertEqual(self.history_repo.watched_episode_keys(session, 5), {(1, 1), (1, 2), (2, 1)})

        original_remove = self.trakt_client.remove_history_items
        self.trakt_client.remove_history_items = lambda _items: (_ for _ in ()).throw(RuntimeError("offline"))
        try:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                service.unmark_scope(
                    title_type="show",
                    trakt_id=5,
                    scope="season",
                    season=1,
                    season_layout="imdb",
                )
        finally:
            self.trakt_client.remove_history_items = original_remove
        with self.db.session() as session:
            self.assertEqual(self.history_repo.watched_episode_keys(session, 5), {(1, 1), (1, 2), (2, 1)})

    def test_history_remove_episode_watch_preserves_rating_and_recalculates_state(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        older = datetime(2026, 7, 1, 12, tzinfo=UTC)
        newer = datetime(2026, 7, 2, 12, tzinfo=UTC)
        with self.db.session() as session:
            title = self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            state = self.user_states.ensure_state(session, title.id)
            state.in_history = True
            state.last_watched_at = newer
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(trakt_id=501, season=1, number=1, title="One"),
                    EpisodeSummary(trakt_id=502, season=1, number=2, title="Two"),
                ],
            )
            self.history_repo.add_event(
                session, trakt_history_id=1, title_trakt_id=5, title="Example", title_type="show",
                action="watched", watched_at=older, season=1, episode=1, rating=8, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=2, title_trakt_id=5, title="Example", title_type="show",
                action="watched", watched_at=newer, season=1, episode=2, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=None, title_trakt_id=5, title="Example", title_type="show",
                action="rated", watched_at=older, season=1, episode=1, rating=8, source="local",
            )

        restore = service.remove_episode_watch(show_trakt_id=5, season=1, episode=2)

        self.assertEqual(self.trakt_client.removed_history_items[0].trakt_id, 502)
        self.assertEqual(restore["watched_at"], newer.isoformat())
        with self.db.session() as session:
            watched = self.history_repo.list_filtered(session, title_type="show", action="watched")
            rated = self.history_repo.list_filtered(session, title_type="show", action="rated")
            state = self.user_states.ensure_state(session, title.id)
            self.assertEqual([(row.season, row.episode) for row in watched], [(1, 1)])
            self.assertEqual(len(rated), 1)
            self.assertTrue(state.in_history)
            self.assertEqual(state.last_watched_at, older.replace(tzinfo=None))

    def test_history_remove_final_show_watch_untracks_show(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        watched_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
        with self.db.session() as session:
            title = self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            state = self.user_states.ensure_state(session, title.id)
            state.in_history = True
            state.tracked = True
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [EpisodeSummary(trakt_id=501, season=1, number=1, title="One")],
            )
            self.history_repo.add_event(
                session, trakt_history_id=1, title_trakt_id=5, title="Example", title_type="show",
                action="watched", watched_at=watched_at, season=1, episode=1, source="trakt",
            )

        service.remove_episode_watch(show_trakt_id=5, season=1, episode=1)

        with self.db.session() as session:
            state = self.user_states.ensure_state(session, title.id)
            self.assertFalse(state.in_history)
            self.assertFalse(state.tracked)

    def test_history_remove_episode_watch_keeps_local_state_when_trakt_fails(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        watched_at = datetime.now(tz=UTC)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [EpisodeSummary(trakt_id=501, season=1, number=1, title="One")],
            )
            self.history_repo.add_event(
                session, trakt_history_id=1, title_trakt_id=5, title="Example", title_type="show",
                action="watched", watched_at=watched_at, season=1, episode=1, source="trakt",
            )
        original_remove = self.trakt_client.remove_history_items
        self.trakt_client.remove_history_items = lambda _items: (_ for _ in ()).throw(RuntimeError("offline"))
        try:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                service.remove_episode_watch(show_trakt_id=5, season=1, episode=1)
        finally:
            self.trakt_client.remove_history_items = original_remove

        with self.db.session() as session:
            self.assertEqual(self.history_repo.watched_episode_keys(session, 5), {(1, 1)})

    def test_history_remove_unknown_date_watch_disables_restore(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=5, title_type="show", title="Example"),
            )
            self.episode_repo.upsert_episode(
                session,
                5,
                EpisodeSummary(trakt_id=501, season=1, number=1, title="Pilot"),
            )
            self.history_repo.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=5,
                title="Example",
                title_type="show",
                action="watched",
                watched_at=datetime(1970, 1, 1, tzinfo=UTC),
                watched_at_known=False,
                season=1,
                episode=1,
                source="local",
            )

        restore = service.remove_episode_watch(show_trakt_id=5, season=1, episode=1)

        self.assertFalse(restore["can_restore"])

    def test_history_remove_movie_scope_preserves_rating_and_can_restore_watch(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        older = datetime(2026, 7, 1, 12, tzinfo=UTC)
        newer = datetime(2026, 7, 3, 12, tzinfo=UTC)
        with self.db.session() as session:
            title = self.titles.upsert_title(session, TitleSummary(trakt_id=7, title_type="movie", title="Movie"))
            state = self.user_states.ensure_state(session, title.id)
            state.in_history = True
            state.rating = 9
            state.last_watched_at = newer
            for history_id, watched_at in ((1, older), (2, newer)):
                self.history_repo.add_event(
                    session, trakt_history_id=history_id, title_trakt_id=7, title="Movie", title_type="movie",
                    action="watched", watched_at=watched_at, source="trakt",
                )
            self.history_repo.add_event(
                session, trakt_history_id=None, title_trakt_id=7, title="Movie", title_type="movie",
                action="rated", watched_at=newer, rating=9, source="local",
            )

        restore = service.remove_watch_scope(title_type="movie", trakt_id=7, scope="title")

        self.assertEqual([item.trakt_id for item in self.trakt_client.removed_history_items], [7])
        self.assertEqual(len(restore["items"]), 1)
        with self.db.session() as session:
            self.assertEqual(self.history_repo.list_filtered(session, title_type="movie", action="watched"), [])
            self.assertEqual(len(self.history_repo.list_filtered(session, title_type="movie", action="rated")), 1)
            state = self.user_states.ensure_state(session, title.id)
            self.assertFalse(state.in_history)
            self.assertEqual(state.rating, 9)

        items = list(restore["items"])
        for item in items:
            item["watched_at"] = datetime.fromisoformat(item["watched_at"])
        service.restore_watch_scope(items=items)

        with self.db.session() as session:
            watched = self.history_repo.list_filtered(session, title_type="movie", action="watched")
            state = self.user_states.ensure_state(session, title.id)
            self.assertEqual(len(watched), 1)
            self.assertTrue(state.in_history)
            self.assertEqual(state.last_watched_at, newer.replace(tzinfo=None))

    def test_history_remove_show_season_scope_keeps_other_seasons(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        watched_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
        with self.db.session() as session:
            title = self.titles.upsert_title(session, TitleSummary(trakt_id=5, title_type="show", title="Example"))
            self.user_states.ensure_state(session, title.id).in_history = True
            self.episode_repo.replace_show_episodes(
                session,
                5,
                [
                    EpisodeSummary(trakt_id=501, season=1, number=1, title="One"),
                    EpisodeSummary(trakt_id=601, season=2, number=1, title="Two"),
                ],
            )
            for history_id, season in ((1, 1), (2, 2)):
                self.history_repo.add_event(
                    session, trakt_history_id=history_id, title_trakt_id=5, title="Example", title_type="show",
                    action="watched", watched_at=watched_at, season=season, episode=1, source="trakt",
                )

        restore = service.remove_watch_scope(title_type="show", trakt_id=5, scope="season", season=1)

        self.assertEqual([item.trakt_id for item in self.trakt_client.removed_history_items], [501])
        self.assertTrue(restore["still_watched"])
        with self.db.session() as session:
            self.assertEqual(self.history_repo.watched_episode_keys(session, 5), {(2, 1)})

    def test_history_restore_rejects_unknown_date_without_remote_rewrite(self) -> None:
        service = HistoryService(
            self.db, self.auth, self.titles, self.user_states, self.history_repo,
            self.episode_repo, self.history_read_model, self.episode_metadata,
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=5, title_type="show", title="Example"),
            )
            self.episode_repo.upsert_episode(
                session,
                5,
                EpisodeSummary(trakt_id=501, season=1, number=1, title="Pilot"),
            )

        with self.assertRaisesRegex(RuntimeError, "unknown date"):
            service.restore_episode_watch(
                show_trakt_id=5,
                title="Example",
                season=1,
                episode=1,
                watched_at=datetime(1970, 1, 1, tzinfo=UTC),
                watched_at_known=False,
            )
        with self.assertRaisesRegex(RuntimeError, "unknown date"):
            service.restore_watch_scope(
                items=[
                    {
                        "title_type": "movie",
                        "trakt_id": 7,
                        "title": "Movie",
                        "season": None,
                        "episode": None,
                        "watched_at": datetime(1970, 1, 1, tzinfo=UTC),
                        "watched_at_known": False,
                    }
                ]
            )

        self.assertEqual(self.trakt_client.history_items, [])

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

    def test_history_sync_keeps_latest_title_watch_when_items_arrive_newest_first(self) -> None:
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        newer = "2026-07-03T12:00:00.000Z"
        older = "2026-07-01T12:00:00.000Z"

        for history_id, watched_at, episode in ((2, newer, 2), (1, older, 1)):
            with self.db.session() as session:
                workflow._import_history_item(
                    session,
                    {
                        "id": history_id,
                        "type": "episode",
                        "watched_at": watched_at,
                        "show": {"title": "Example", "ids": {"trakt": 5}},
                        "episode": {
                            "title": f"Episode {episode}",
                            "season": 1,
                            "number": episode,
                            "ids": {"trakt": 500 + episode},
                        },
                    },
                )

        with self.db.session() as session:
            state = self.user_states.progress_state(session, 5)

        self.assertIsNotNone(state)
        self.assertEqual(state.last_watched_at, datetime(2026, 7, 3, 12))

    def test_history_sync_keeps_latest_repeat_watch_when_items_arrive_newest_first(self) -> None:
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        for history_id, watched_at in (
            (2, "2026-07-03T12:00:00.000Z"),
            (1, "2026-07-01T12:00:00.000Z"),
        ):
            with self.db.session() as session:
                workflow._import_history_item(
                    session,
                    {
                        "id": history_id,
                        "type": "episode",
                        "watched_at": watched_at,
                        "show": {"title": "Example", "ids": {"trakt": 5}},
                        "episode": {
                            "title": "Pilot",
                            "season": 1,
                            "number": 1,
                            "ids": {"trakt": 501},
                        },
                    },
                )

        with self.db.session() as session:
            rows = self.history_repo.list_filtered(session, title_type="show", action="watched")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].trakt_history_id, 2)
        self.assertEqual(rows[0].watched_at, datetime(2026, 7, 3, 12))

    def test_history_sync_reconciliation_removes_absent_remote_watch_and_resets_state(self) -> None:
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        with self.db.session() as session:
            title = self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=37705, title_type="show", title="Fullmetal Alchemist"),
            )
            self.user_states.ensure_state(session, title.id).in_history = True
            self.history_repo.add_event(
                session,
                trakt_history_id=13890790464,
                title_trakt_id=37705,
                title="Fullmetal Alchemist",
                title_type="show",
                action="watched",
                watched_at=datetime.now(tz=UTC),
                season=1,
                episode=13,
                source="trakt",
            )

        removed = workflow._sync_history_and_ratings(
            [],
            [],
            reconciliation_scopes=[
                _HistoryReconciliationScope(
                    title_type="show",
                    present_history_ids=set(),
                    watched_at_cutoff=None,
                )
            ],
        )

        with self.db.session() as session:
            watched_rows = self.history_repo.list_filtered(session, action="watched")
            state = self.user_states.ensure_state(session, title.id)
        self.assertEqual(removed, 1)
        self.assertEqual(watched_rows, [])
        self.assertFalse(state.in_history)

    def test_history_sync_reconciliation_recomputes_last_watch_and_refreshes_progress(self) -> None:
        progress_repo = ProgressRepository()
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            progress_repo,
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        older = datetime(2026, 7, 1, 12, tzinfo=UTC)
        newer = datetime(2026, 7, 3, 12, tzinfo=UTC)
        with self.db.session() as session:
            title = self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=5, title_type="show", title="Example"),
            )
            state = self.user_states.ensure_state(session, title.id)
            state.in_history = True
            state.tracked = True
            state.last_watched_at = newer
            self.history_repo.add_event(
                session,
                trakt_history_id=1,
                title_trakt_id=5,
                title="Example",
                title_type="show",
                action="watched",
                watched_at=older,
                season=1,
                episode=1,
                source="trakt",
            )
            self.history_repo.add_event(
                session,
                trakt_history_id=2,
                title_trakt_id=5,
                title="Example",
                title_type="show",
                action="watched",
                watched_at=newer,
                season=1,
                episode=2,
                source="trakt",
            )
        refreshes: list[tuple[int, bool]] = []
        workflow.refresh_show = lambda trakt_id, *, fresh=False: refreshes.append((trakt_id, fresh))

        workflow._sync_history_and_ratings(
            [],
            [],
            reconciliation_scopes=[
                _HistoryReconciliationScope(
                    title_type="show",
                    present_history_ids={1},
                    watched_at_cutoff=None,
                )
            ],
            run_enrichment=False,
        )

        with self.db.session() as session:
            state = self.user_states.progress_state(session, 5)
        self.assertIsNotNone(state)
        self.assertTrue(state.in_history)
        self.assertEqual(state.last_watched_at, older.replace(tzinfo=None))
        self.assertEqual(refreshes, [(5, True)])

    def test_full_history_reconciliation_returns_remote_items_for_recovery(self) -> None:
        remote_item = {
            "id": 10,
            "type": "episode",
            "watched_at": "2026-07-01T12:00:00.000Z",
            "show": {"title": "Example", "ids": {"trakt": 5}},
            "episode": {
                "title": "Pilot",
                "season": 1,
                "number": 1,
                "ids": {"trakt": 501},
            },
        }

        class _HistoryClient:
            def get_watch_history_page(self, *, title_type=None, limit=1000, page=1):
                batch = [remote_item] if title_type is None and page == 1 else []
                return batch, {"x-pagination-page-count": "1"}

        items, scopes = HistorySyncWorkflow._fetch_full_history_reconciliation(_HistoryClient())

        self.assertEqual([item["id"] for item in items], [10])
        self.assertEqual(
            next(scope.present_history_ids for scope in scopes if scope.title_type == "show"),
            {10},
        )

    def test_history_sync_removes_local_rating_residue_absent_from_trakt(self) -> None:
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        with self.db.session() as session:
            title = self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=37705, title_type="show", title="Fullmetal Alchemist"),
            )
            state = self.user_states.ensure_state(session, title.id)
            state.rating = 7
            self.history_repo.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=37705,
                title="Fullmetal Alchemist",
                title_type="show",
                action="rated",
                watched_at=datetime.now(tz=UTC),
                season=1,
                episode=7,
                rating=7,
                source="local",
            )

        workflow._sync_history_and_ratings([], [], run_enrichment=False)

        with self.db.session() as session:
            rated_rows = self.history_repo.list_filtered(session, action="rated")
            state = self.user_states.ensure_state(session, title.id)
        self.assertEqual(rated_rows, [])
        self.assertIsNone(state.rating)

    def test_recent_history_reconciliation_only_covers_scanned_time_window(self) -> None:
        now = datetime.now(tz=UTC)

        class _HistoryClient:
            def get_watch_history(self, title_type: str | None = None, limit: int = 100, page: int = 1) -> list[dict]:
                if title_type is None:
                    return [
                        {
                            "id": 10,
                            "type": "episode",
                            "watched_at": now.isoformat().replace("+00:00", "Z"),
                            "show": {"ids": {"trakt": 1}},
                            "episode": {"season": 1, "number": 1},
                        }
                    ]
                return []

        with self.db.session() as session:
            self.history_repo.add_event(
                session,
                trakt_history_id=10,
                title_trakt_id=1,
                title="Show",
                title_type="show",
                action="watched",
                watched_at=now,
                season=1,
                episode=1,
                source="trakt",
            )
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )

        items, scopes = workflow._fetch_recent_history_updates(_HistoryClient(), page_size=1)

        self.assertEqual(items, [])
        show_scope = next(scope for scope in scopes if scope.title_type == "show")
        self.assertEqual(show_scope.present_history_ids, {10})
        self.assertEqual(show_scope.watched_at_cutoff, now)
        movie_scope = next(scope for scope in scopes if scope.title_type == "movie")
        self.assertIsNone(movie_scope.watched_at_cutoff)

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

    def test_history_title_summaries_sort_before_pagination_with_nulls_last(self) -> None:
        items = [
            (10, "Twin", 2020, datetime(2026, 4, 1, tzinfo=UTC), 8, True),
            (20, "Twin", 2024, datetime(2026, 4, 3, tzinfo=UTC), 8, True),
            (30, "Unknown", None, datetime(1970, 1, 1, tzinfo=UTC), None, False),
        ]
        with self.db.session() as session:
            for trakt_id, title, year, watched_at, rating, watched_at_known in items:
                self.titles.upsert_title(
                    session,
                    TitleSummary(trakt_id=trakt_id, title_type="movie", title=title, year=year),
                )
                self.history_repo.add_event(
                    session,
                    trakt_history_id=1000 + trakt_id,
                    title_trakt_id=trakt_id,
                    title=title,
                    title_type="movie",
                    action="watched",
                    watched_at=watched_at,
                    watched_at_known=watched_at_known,
                    rating=rating,
                )

        rating_rows = self.history_read_model.history_title_summaries(
            sort_by="rating",
            sort_direction="desc",
        )
        release_page = self.history_read_model.history_title_summaries(
            sort_by="release_year",
            sort_direction="desc",
            limit=1,
            offset=1,
        )
        watched_rows = self.history_read_model.history_title_summaries(
            sort_by="last_watched",
            sort_direction="asc",
        )

        self.assertEqual([row["title_trakt_id"] for row in rating_rows], [10, 20, 30])
        self.assertEqual([row["title_trakt_id"] for row in release_page], [10])
        self.assertEqual([row["title_trakt_id"] for row in watched_rows], [10, 20, 30])
        self.assertEqual([row["title_year"] for row in rating_rows], [2020, 2024, None])

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

    def test_interaction_service_does_not_acknowledge_new_when_history_write_fails(self) -> None:
        class _FailingHistory(_FakeHistoryService):
            def add_history_item(self, item: HistoryItemInput) -> None:
                raise RuntimeError("Trakt unavailable")

        notifications = _FakeNotificationService()
        service = InteractionService(_FailingHistory(), notifications, _FakeProgressService())
        snapshot = ProgressSnapshot(
            trakt_id=5,
            title="Severance",
            completed=1,
            aired=2,
            percent_completed=50.0,
            next_episode=EpisodeSummary(trakt_id=55, season=2, number=3, title="Who Is Alive?"),
        )

        with self.assertRaisesRegex(RuntimeError, "Trakt unavailable"):
            service.mark_progress_episode_watched(snapshot)

        self.assertEqual(notifications.seen, [])

    def test_notification_poll_refreshes_progress_before_matching_calendar(self) -> None:
        calls: list[tuple[str, dict]] = []
        progress = SimpleNamespace(
            sync_progress=lambda **kwargs: calls.append(("progress", kwargs))
        )
        service = NotificationService(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            progress_service=progress,
        )
        service._workflow = SimpleNamespace(
            poll_upcoming=lambda **kwargs: calls.append(("notifications", kwargs)) or []
        )
        service._release_tracking = None
        service.refresh_pending_sources = lambda: []

        service.poll_upcoming(send_native=False)

        self.assertEqual(
            calls,
            [
                ("progress", {"dropped_only": False}),
                ("notifications", {"send_native": False}),
            ],
        )

    def test_sync_trakt_data_runs_full_progress_before_notification_refresh(self) -> None:
        calls: list[tuple[str, dict]] = []
        status_callback = lambda _message: None
        service = object.__new__(SyncService)
        service._enrich_queue = None
        service.refresh_history = lambda **kwargs: calls.append(("history", kwargs))
        service._progress_service = SimpleNamespace(
            sync_progress=lambda **kwargs: calls.append(("progress", kwargs))
        )
        service._notification_service = SimpleNamespace(
            refresh_pending_sources=lambda: calls.append(("notifications", {}))
        )

        service.sync_trakt_data(status_callback=status_callback)

        self.assertEqual(
            calls,
            [
                (
                    "history",
                    {
                        "force_full_assets": False,
                        "status_callback": status_callback,
                    },
                ),
                (
                    "progress",
                    {
                        "dropped_only": False,
                        "force_refresh": True,
                        "force_full_assets": False,
                    },
                ),
                ("notifications", {}),
            ],
        )

    def test_episode_metadata_reads_active_profile_trakt_cache(self) -> None:
        calls: list[str] = []
        original = episode_metadata_module.load_cached_trakt_rating_items
        self.auth.config.active_slug = "viewer"
        episode_metadata_module.load_cached_trakt_rating_items = lambda slug: calls.append(slug) or []
        try:
            self.episode_metadata.load_cached_trakt_rating_maps()
        finally:
            episode_metadata_module.load_cached_trakt_rating_items = original

        self.assertEqual(calls, ["viewer"])

    def test_legacy_history_repair_reads_active_profile_trakt_cache(self) -> None:
        workflow = HistorySyncWorkflow(
            self.db,
            self.auth,
            self.titles,
            self.user_states,
            self.history_repo,
            ProgressRepository(),
            self.episode_repo,
            self.sync_state,
            self.imdb,
            OperationLog(),
            self.episode_metadata,
        )
        with self.db.session() as session:
            self.history_repo.add_event(
                session,
                trakt_history_id=10,
                title_trakt_id=501,
                title="Legacy episode",
                title_type="episode",
                action="watched",
                watched_at=datetime.now(tz=UTC),
                source="trakt",
            )
        self.auth.config.active_slug = "viewer"
        original = history_sync_module.load_cached_trakt_history_items
        history_sync_module.load_cached_trakt_history_items = (
            lambda slug: (_ for _ in ()).throw(RuntimeError(f"profile:{slug}"))
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "profile:viewer"):
                workflow.repair_legacy_episode_history()
        finally:
            history_sync_module.load_cached_trakt_history_items = original

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
        self.assertTrue(AppConfig().web_imdb_seasons_enabled)
        self.assertFalse(AppConfig().web_hide_spoilers)

    def test_config_loads_old_file_with_imdb_seasons_enabled(self) -> None:
        path = Path(self.tmpdir.name) / "legacy-config.json"
        path.write_text('{"client_id": "legacy"}', encoding="utf-8")
        store = ConfigStore(path)

        config = store.load()

        self.assertEqual(config.client_id, "legacy")
        self.assertTrue(config.web_imdb_seasons_enabled)
        self.assertFalse(config.web_hide_spoilers)
        config.web_imdb_seasons_enabled = False
        config.web_hide_spoilers = True
        store.save(config)
        self.assertFalse(store.load().web_imdb_seasons_enabled)
        self.assertTrue(store.load().web_hide_spoilers)

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

    def test_sync_service_skips_known_episode_reenrichment_when_imdb_dataset_is_unchanged(self) -> None:
        config_store = ConfigStore(Path(self.tmpdir.name) / "config.json")
        services = build_services(config_store, self.db)
        repair_calls: list[bool] = []
        services.sync._imdb_client.sync = lambda force=False, status_callback=None: False
        services.sync._episode_metadata.backfill_episode_imdb_ids_from_payloads = lambda payloads: None
        services.sync._episode_metadata.enrich_episode_imdb_ratings = lambda: (_ for _ in ()).throw(
            AssertionError("idle IMDb resolver pass")
        )
        services.sync._episode_metadata.repair_episode_imdb_ratings = lambda: repair_calls.append(True) or 0

        self.assertFalse(services.sync.sync_imdb_dataset())
        self.assertEqual(repair_calls, [True])

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
            def get_season_episode_still_urls(self, show_tmdb_id: int, season: int) -> dict[int, str]:
                return {}

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

    def test_enrich_episode_stills_uses_one_tmdb_request_per_season(self) -> None:
        tmdb = _FakeTmdbClient()
        episode_metadata = EpisodeMetadataService(
            self.db,
            self.episode_repo,
            self.imdb,
            self.titles,
            self.auth,
            lambda _config: tmdb,
        )
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=138748, title_type="show", title="The Capture", tmdb_id=250487),
            )
            for number in (1, 2, 3):
                self.episode_repo.upsert_episode(
                    session,
                    138748,
                    EpisodeSummary(trakt_id=300 + number, season=2, number=number, title=f"Episode {number}"),
                )

        changed = episode_metadata.enrich_episode_stills(
            [(138748, 2, 1), (138748, 2, 2), (138748, 2, 3)],
            trigger=TRIGGER_PAGE_CONTEXT,
            requested_parts=("still",),
        )

        self.assertTrue(changed)
        self.assertEqual(tmdb.season_still_calls, [(250487, 2)])

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
            title = self.titles.get_title(session, 138748)
            assert title is not None
            state = self.user_states.ensure_state(session, title.id)
            self.assertEqual(row.trakt_details_status, "unknown")
            self.assertIsNone(state.rating)

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
            episode_row = self.episode_repo.upsert_episode(
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
            episode_row.imdb_season = 4
            episode_row.imdb_episode = 1
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
        self.assertEqual(items[0].next_episode.imdb_season, 4)
        self.assertEqual(items[0].next_episode.imdb_episode, 1)
        self.assertEqual(self.trakt_client.title_details_calls, [])
        self.assertEqual(self.trakt_client.episode_details_calls, [])

    def test_progress_rating_averages_are_limited_to_visible_shows(self) -> None:
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
            history_repo=self.history_repo,
        )
        now = datetime.now(tz=UTC)
        with self.db.session() as session:
            ProgressRepository().upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=5,
                    title="Visible",
                    completed=2,
                    aired=3,
                    percent_completed=66.0,
                    next_episode=EpisodeSummary(trakt_id=503, season=1, number=3, title="Three"),
                ),
            )
            self.history_repo.add_event(
                session, trakt_history_id=1, title_trakt_id=5, title="Visible", title_type="show",
                action="watched", watched_at=now, season=1, episode=1, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=2, title_trakt_id=5, title="Visible", title_type="show",
                action="rated", watched_at=now, season=1, episode=1, rating=8, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=3, title_trakt_id=5, title="Visible", title_type="show",
                action="watched", watched_at=now, season=1, episode=2, rating=6, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=4, title_trakt_id=5, title="Visible", title_type="show",
                action="rated", watched_at=now, season=1, episode=2, rating=9, source="trakt",
            )
            self.history_repo.add_event(
                session, trakt_history_id=5, title_trakt_id=99, title="Unrelated", title_type="show",
                action="watched", watched_at=now, season=1, episode=1, rating=10, source="trakt",
            )

        self.episode_metadata.load_cached_trakt_rating_maps = lambda: (_ for _ in ()).throw(AssertionError("cache scan"))
        items = workflow.dashboard_progress()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title_episode_avg_rating, 7.0)

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
                    trakt_rating=8.2,
                    trakt_votes=1200,
                    imdb_rating=8.4,
                    imdb_votes=3400,
                ),
            )
        matrix = service.load_show_matrix(138748)
        self.assertTrue(matrix.has_episodes)
        self.assertEqual((matrix.title_trakt_rating, matrix.title_trakt_votes), (8.2, 1200))
        self.assertEqual((matrix.title_imdb_rating, matrix.title_imdb_votes), (8.4, 3400))
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
        self.assertEqual([season.label for season in matrix.imdb_seasons], ["S1", "S2", "ALL"])

    def test_episode_ratings_matrix_builds_separate_imdb_season_layout(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=198225, title_type="show", title="Frieren"))
            season_one = self.episode_repo.upsert_episode(
                session,
                198225,
                EpisodeSummary(
                    trakt_id=2801,
                    season=1,
                    number=1,
                    title="Episode 1",
                    imdb_rating=8.1,
                    imdb_votes=100,
                    imdb_season=1,
                    imdb_episode=1,
                ),
            )
            season_one.imdb_season = 1
            season_one.imdb_episode = 1
            season_two = self.episode_repo.upsert_episode(
                session,
                198225,
                EpisodeSummary(
                    trakt_id=2901,
                    season=1,
                    number=29,
                    title="Episode 29",
                    imdb_rating=8.6,
                    imdb_votes=900,
                    imdb_season=2,
                    imdb_episode=1,
                ),
            )
            season_two.imdb_season = 2
            season_two.imdb_episode = 1
            season_three = self.episode_repo.upsert_episode(
                session,
                198225,
                EpisodeSummary(
                    trakt_id=3901,
                    season=1,
                    number=39,
                    title="Episode 39",
                    imdb_season=3,
                    imdb_episode=1,
                ),
            )
            season_three.imdb_season = 3
            season_three.imdb_episode = 1

        matrix = service.load_show_matrix(198225, allow_network_refresh=False)

        self.assertEqual([season.label for season in matrix.seasons], ["S1", "ALL"])
        self.assertEqual([season.label for season in matrix.imdb_seasons], ["S1", "S2", "S3", "ALL"])
        self.assertEqual([row.label for row in matrix.imdb_rows], ["E1"])
        self.assertEqual([cell.display_value for cell in matrix.imdb_rows[0].cells], ["8.1", "8.6", "?"])
        self.assertEqual(matrix.imdb_rows[0].cells[1].tooltip, "Episode 29\nS02 E01\n900 votes")

    def test_episode_ratings_matrix_resolves_imdb_id_conflict_without_moving_rating_to_wrong_episode(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_ids[("tt1190634", 5, 4)] = "tt33023431"
        self.imdb.episode_ids[("tt1190634", 5, 5)] = "tt33023447"
        self.imdb.episode_ids[("tt1190634", 5, 6)] = "tt33023477"
        self.imdb.episode_metadata["tt33023431"] = self._imdb_episode_metadata(parent="tt1190634", season=5, episode=4, title="King of Hell", rating=7.8, votes=15500)
        self.imdb.episode_metadata["tt33023447"] = self._imdb_episode_metadata(parent="tt1190634", season=5, episode=5, title="One-Shots", rating=None, votes=None)
        self.imdb.episode_metadata["tt33023477"] = self._imdb_episode_metadata(parent="tt1190634", season=5, episode=6, title="Though the Heavens Fall", rating=None, votes=None)
        self.trakt_client.get_show_episodes = lambda trakt_id: [
            EpisodeSummary(trakt_id=12138432, season=5, number=4, title="King of Hell", imdb_id="tt33023477"),
            EpisodeSummary(trakt_id=12138433, season=5, number=5, title="One-Shots", imdb_id="tt33023447"),
            EpisodeSummary(trakt_id=12138434, season=5, number=6, title="Episode 6", imdb_id="tt33023431"),
        ]
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=139960, title_type="show", title="The Boys", imdb_id="tt1190634"))

        matrix = service.load_show_matrix(139960, force_refresh=True)

        self.assertEqual(matrix.rows[3].cells[0].display_value, "7.8")
        self.assertEqual(matrix.rows[3].cells[0].imdb_url, "https://www.imdb.com/title/tt33023431")
        self.assertEqual(matrix.rows[4].cells[0].display_value, "?")
        self.assertEqual(matrix.rows[4].cells[0].imdb_url, "https://www.imdb.com/title/tt33023447")
        self.assertEqual(matrix.rows[5].cells[0].display_value, "?")
        self.assertEqual(matrix.rows[5].cells[0].imdb_url, "https://www.imdb.com/title/tt33023477")

    def test_episode_ratings_matrix_force_refresh_repairs_stale_cached_imdb_fields(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_ids[("tt1190634", 5, 4)] = "tt33023431"
        self.imdb.episode_metadata["tt33023431"] = self._imdb_episode_metadata(parent="tt1190634", season=5, episode=4, title="King of Hell", rating=7.8, votes=15500)
        self.imdb.episode_metadata["tt33023477"] = self._imdb_episode_metadata(parent="tt1190634", season=5, episode=6, title="Though the Heavens Fall", rating=None, votes=None)
        self.trakt_client.get_show_episodes = lambda trakt_id: [
            EpisodeSummary(trakt_id=12138432, season=5, number=4, title="King of Hell", imdb_id="tt33023477"),
        ]
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=139960, title_type="show", title="The Boys", imdb_id="tt1190634"))
            self.episode_repo.upsert_episode(
                session,
                139960,
                EpisodeSummary(trakt_id=12138432, season=5, number=4, title="King of Hell", imdb_id="tt33023477", imdb_rating=None, imdb_votes=None),
            )

        matrix = service.load_show_matrix(139960, force_refresh=True)

        self.assertEqual(matrix.rows[3].cells[0].display_value, "7.8")
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 139960, 5, 4)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.imdb_id, "tt33023431")
            self.assertEqual(row.imdb_rating, 7.8)
            self.assertEqual(row.imdb_votes, 15500)

    def test_episode_ratings_matrix_reconciles_unmapped_episode_once_without_network_refresh(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_ids[("tt22248376", 1, 28)] = "tt33300000"
        self.imdb.episode_ids[("tt22248376", 2, 1)] = "tt33300001"
        self.imdb.episode_metadata["tt33300001"] = self._imdb_episode_metadata(
            parent="tt22248376",
            season=2,
            episode=1,
            title="Episode 29",
            rating=8.6,
            votes=900,
        )
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=198225, title_type="show", title="Frieren", imdb_id="tt22248376"),
            )
            self.episode_repo.upsert_episode(
                session,
                198225,
                EpisodeSummary(trakt_id=2901, season=1, number=29, title="Episode 29"),
            )

        first = service.load_show_matrix(198225, allow_network_refresh=False)
        first_lookup_count = len(self.imdb.episode_lookup_calls)
        second = service.load_show_matrix(198225, allow_network_refresh=False)

        self.assertEqual(first.rows[28].cells[0].display_value, "8.6")
        self.assertEqual(first.rows[28].label, "E29")
        self.assertEqual([season.label for season in first.imdb_seasons], ["S2", "ALL"])
        self.assertEqual(first.imdb_rows[0].cells[0].display_value, "8.6")
        self.assertEqual(second.rows[28].cells[0].display_value, "8.6")
        self.assertGreater(first_lookup_count, 0)
        self.assertEqual(len(self.imdb.episode_lookup_calls), first_lookup_count)
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 198225, 1, 29)
            self.assertEqual(row.imdb_id, "tt33300001")
            self.assertEqual(row.imdb_match_status, "resolved")

    def test_episode_ratings_matrix_retries_no_match_only_after_imdb_revision_changes(self) -> None:
        self.imdb.ready = True
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=198225, title_type="show", title="Frieren", imdb_id="tt22248376"),
            )
            self.episode_repo.upsert_episode(
                session,
                198225,
                EpisodeSummary(trakt_id=3901, season=1, number=39, title="Episode 39"),
            )

        service.load_show_matrix(198225, allow_network_refresh=False)
        first_lookup_count = len(self.imdb.episode_lookup_calls)
        service.load_show_matrix(198225, allow_network_refresh=False)
        self.assertGreater(first_lookup_count, 0)
        self.assertEqual(len(self.imdb.episode_lookup_calls), first_lookup_count)

        self.imdb.revision = "imdb-revision-2"
        self.imdb.episode_ids[("tt22248376", 1, 39)] = "tt41157278"
        self.imdb.episode_metadata["tt41157278"] = self._imdb_episode_metadata(
            parent="tt22248376",
            season=1,
            episode=39,
            title="Episode 39",
            rating=None,
            votes=None,
        )
        matrix = service.load_show_matrix(198225, allow_network_refresh=False)

        self.assertGreater(len(self.imdb.episode_lookup_calls), first_lookup_count)
        self.assertEqual(matrix.rows[38].cells[0].display_value, "?")
        self.assertEqual(matrix.rows[38].cells[0].imdb_url, "https://www.imdb.com/title/tt41157278")
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 198225, 1, 39)
            self.assertEqual(row.imdb_match_status, "resolved")

    def test_episode_ratings_matrix_does_not_remap_known_imdb_id_without_rating(self) -> None:
        self.imdb.ready = True
        self.imdb.episode_metadata["tt41157278"] = self._imdb_episode_metadata(
            parent="tt22248376",
            season=3,
            episode=1,
            title="Episode 39",
            rating=None,
            votes=None,
        )
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=198225, title_type="show", title="Frieren", imdb_id="tt22248376"),
            )
            self.episode_repo.upsert_episode(
                session,
                198225,
                EpisodeSummary(trakt_id=3901, season=1, number=39, title="Episode 39", imdb_id="tt41157278"),
            )

        matrix = service.load_show_matrix(198225, allow_network_refresh=False)
        first_revision_calls = self.imdb.revision_calls
        service.load_show_matrix(198225, allow_network_refresh=False)

        self.assertEqual(self.imdb.episode_lookup_calls, [])
        self.assertGreater(first_revision_calls, 0)
        self.assertEqual(self.imdb.revision_calls, first_revision_calls)
        self.assertEqual(matrix.rows[38].cells[0].imdb_url, "https://www.imdb.com/title/tt41157278")
        self.assertEqual([season.label for season in matrix.imdb_seasons], ["S3", "ALL"])
        self.assertEqual(matrix.imdb_rows[0].cells[0].imdb_url, "https://www.imdb.com/title/tt41157278")
        with self.db.session() as session:
            row = self.episode_repo.find_episode(session, 198225, 1, 39)
            self.assertEqual(row.imdb_match_status, "resolved")
            self.assertEqual((row.imdb_season, row.imdb_episode), (3, 1))

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
                EpisodeSummary(
                    trakt_id=101,
                    season=1,
                    number=1,
                    title="E1",
                    imdb_rating=9.2,
                    imdb_votes=1134,
                    first_aired=datetime.now(tz=UTC) - timedelta(days=2),
                ),
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
                EpisodeSummary(
                    trakt_id=101,
                    season=1,
                    number=1,
                    title="E1",
                    trakt_rating=8.2,
                    trakt_votes=2300,
                    first_aired=datetime.now(tz=UTC) - timedelta(days=2),
                ),
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

    def test_episode_ratings_matrix_does_not_refresh_old_ready_trakt_values(self) -> None:
        service = EpisodeRatingsMatrixService(self.db, self.auth, self.titles, self.history_repo, self.episode_repo, self.imdb)
        with self.db.session() as session:
            self.titles.upsert_title(session, TitleSummary(trakt_id=138748, title_type="show", title="The Capture"))
            row = self.episode_repo.upsert_episode(
                session,
                138748,
                EpisodeSummary(
                    trakt_id=101,
                    season=1,
                    number=1,
                    title="E1",
                    trakt_rating=8.2,
                    trakt_votes=2300,
                    first_aired=datetime.now(tz=UTC) - timedelta(days=30),
                ),
            )
            row.trakt_details_status = ENRICH_STATUS_READY
            row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=6)
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

    def test_sync_assets_repair_leaves_binary_artwork_warming_to_the_idle_queue(self) -> None:
        enriched: list[tuple[int, str]] = []
        warmed: list[str] = []
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
            SimpleNamespace(enrich_title_key=lambda trakt_id, title_type, **_kwargs: enriched.append((trakt_id, title_type)) or "ready"),
            None,
        )
        service._image_cache = _FakeImageCache()
        with self.db.session() as session:
            row = self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=138748,
                    title_type="show",
                    title="The Capture",
                    poster_url="https://poster.example/capture.jpg",
                ),
            )
            row.poster_status = ENRICH_STATUS_READY
        original_warm = services_module.warm_image_urls
        services_module.warm_image_urls = lambda _cache, urls, **_kwargs: warmed.extend(urls) or {"selected": len(urls), "warmed": len(urls), "failed": 0, "skipped": 0}
        try:
            service.sync_assets_repair()
        finally:
            services_module.warm_image_urls = original_warm

        self.assertEqual(enriched, [])
        self.assertEqual(warmed, [])

    def test_sync_assets_repair_skips_ready_poster_when_binary_cache_exists(self) -> None:
        enriched: list[tuple[int, str]] = []
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
            SimpleNamespace(enrich_title_key=lambda trakt_id, title_type, **_kwargs: enriched.append((trakt_id, title_type)) or "ready"),
            None,
        )
        service._image_cache = _FakeImageCache({"https://poster.example/capture.jpg"})
        with self.db.session() as session:
            row = self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=138748,
                    title_type="show",
                    title="The Capture",
                    poster_url="https://poster.example/capture.jpg",
                ),
            )
            row.poster_status = ENRICH_STATUS_READY

        service.sync_assets_repair()

        self.assertEqual(enriched, [])

    def test_metadata_backfill_is_bounded_and_does_not_scan_binary_artwork(self) -> None:
        enriched: list[tuple[int, str]] = []
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
            SimpleNamespace(enrich_title_key=lambda trakt_id, title_type, **_kwargs: enriched.append((trakt_id, title_type)) or "ready"),
            None,
        )
        with self.db.session() as session:
            for trakt_id in range(1, 101):
                self.titles.upsert_title(
                    session,
                    TitleSummary(trakt_id=trakt_id, title_type="movie", title=f"Movie {trakt_id}"),
                )

        service.sync_assets_backfill()

        self.assertEqual(len(enriched), 80)
        self.assertEqual(enriched[0], (1, "movie"))
        self.assertEqual(enriched[-1], (80, "movie"))

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
