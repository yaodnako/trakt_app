from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event

from trakt_tracker.application.tmdb_catalog import TmdbCatalogItem, TmdbCatalogService
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot, TitleSummary
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.models import EpisodeCache, HistoryEvent, WatchProgress
from trakt_tracker.persistence.repositories import SyncStateRepository, TitleRepository
from trakt_tracker.persistence.tmdb_preview import TmdbPreviewRepository


class _FakeTmdbClient:
    def __init__(self) -> None:
        self.cached_seasons: dict[tuple[int, int], dict] = {}
        self.remote_seasons: dict[tuple[int, int], dict] = {}
        self.remote_season_calls: list[tuple[int, int]] = []

    def is_configured(self) -> bool:
        return True

    def get_catalog_details(self, title_type: str, tmdb_id: int) -> dict:
        return {
            "id": int(tmdb_id),
            "title": "Preview movie" if title_type == "movie" else "Preview show",
            "name": "Preview show" if title_type == "show" else "",
            "overview": "A local preview",
            "release_date": "2026-01-01" if title_type == "movie" else "",
            "first_air_date": "2026-01-01" if title_type == "show" else "",
            "vote_average": 8.1,
            "vote_count": 42,
            "external_ids": {"imdb_id": "tt0133093"},
        }

    def get_cached_catalog_season(self, tmdb_id: int, season: int) -> dict | None:
        return self.cached_seasons.get((int(tmdb_id), int(season)))

    def get_catalog_season(self, tmdb_id: int, season: int) -> dict:
        key = (int(tmdb_id), int(season))
        self.remote_season_calls.append(key)
        return self.remote_seasons.get(key, {"episodes": []})

    def refresh_catalog_season(self, tmdb_id: int, season: int) -> dict:
        return self.get_catalog_season(tmdb_id, season)

    def trending_catalog(self, title_type: str, *, page: int = 1) -> dict:
        return self._catalog_page(title_type, page=page)

    def discover_catalog(self, title_type: str, *, page: int = 1, upcoming: bool = False) -> dict:
        return self._catalog_page(title_type, page=page)

    @staticmethod
    def _catalog_page(title_type: str, *, page: int) -> dict:
        return {
            "page": page,
            "total_pages": 2,
            "results": [
                {
                    "id": 101,
                    "title": "Preview movie" if title_type == "movie" else "",
                    "name": "Preview show" if title_type == "show" else "",
                    "release_date": "2026-01-01" if title_type == "movie" else "",
                    "first_air_date": "2026-01-01" if title_type == "show" else "",
                    "vote_average": 8.1,
                    "vote_count": 42,
                    "popularity": 94.5,
                }
            ],
        }


class _FakeFactory:
    def __init__(self) -> None:
        self.client = _FakeTmdbClient()

    def __call__(self, _config):
        return self.client


class _FakeImdbClient:
    def is_ready(self) -> bool:
        return True

    def enrich_title(self, title):
        title.imdb_rating = 8.7
        title.imdb_votes = 2_100_000
        return title


class _FakeOutbox:
    def __init__(self) -> None:
        self.memberships: list[dict] = []
        self.histories: list[dict] = []
        self.wake_count = 0

    def enqueue_membership(self, _session, **kwargs):
        self.memberships.append(kwargs)
        return f"membership:{len(self.memberships)}"

    def enqueue_history(self, _session, **kwargs):
        self.histories.append(kwargs)
        return f"history:{len(self.histories)}"

    def wake(self) -> None:
        self.wake_count += 1


class _FakeSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, message) -> None:
        self.messages.append((message.title, message.body))


class TmdbPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmpdir.name) / "preview.sqlite3")
        self.db.create_schema()
        self.repo = TmdbPreviewRepository()
        self.titles = TitleRepository()
        self.auth = SimpleNamespace(
            config=AppConfig(tmdb_read_access_token="token", notifications_enabled=True, notification_release_delay_minutes=0, movie_release_notification_delay_minutes=0),
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def test_old_config_uses_trakt_default_and_invalid_mode_is_normalized(self) -> None:
        path = Path(self.tmpdir.name) / "config.json"
        path.write_text(
            '{"client_id":"old","retired_provider_key":"stale"}',
            encoding="utf-8",
        )
        store = ConfigStore(path)
        loaded = store.load()
        self.assertEqual(loaded.catalog_provider_mode, "trakt")
        self.assertEqual(loaded.network_proxy_url, "")
        store.save(loaded)
        self.assertNotIn("retired_provider_key", path.read_text(encoding="utf-8"))
        config = AppConfig(catalog_provider_mode="not-a-provider")
        store.save(config)
        self.assertEqual(store.load().catalog_provider_mode, "trakt")

        config.network_proxy_url = "socks5h://127.0.0.1:10808"
        store.save(config)
        self.assertEqual(store.load().network_proxy_url, "socks5h://127.0.0.1:10808")

    def test_preview_intent_is_atomic_coalesced_and_survives_restart(self) -> None:
        with self.db.session() as session:
            self.repo.upsert_snapshot(session, {"title_type": "movie", "tmdb_id": 101, "title": "Movie"})
            self.repo.set_intent(
                session,
                operation_type="watchlist",
                title_type="movie",
                tmdb_id=101,
                desired=True,
                payload={"title": "Movie"},
            )
        with self.db.session() as session:
            row = self.repo.intent(session, operation_type="watchlist", title_type="movie", tmdb_id=101)
            self.assertIsNotNone(row)
            self.assertTrue(self.repo.effective_state(session, operation_type="watchlist", title_type="movie", tmdb_id=101))
            self.repo.set_intent(
                session,
                operation_type="watchlist",
                title_type="movie",
                tmdb_id=101,
                desired=False,
                payload={},
            )
        with self.db.session() as session:
            self.assertIsNone(self.repo.intent(session, operation_type="watchlist", title_type="movie", tmdb_id=101))
            self.repo.set_intent(
                session,
                operation_type="history",
                title_type="movie",
                tmdb_id=101,
                desired=True,
                payload={"watched_at": datetime.now(tz=UTC).isoformat()},
            )
        self.db.close()
        self.db = Database(Path(self.tmpdir.name) / "preview.sqlite3")
        self.db.create_schema()
        with self.db.session() as session:
            self.assertEqual(len(self.repo.list_intents(session)), 1)

    def test_unmapped_actions_are_local_and_mapping_moves_intent_to_outbox(self) -> None:
        outbox = _FakeOutbox()
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo, outbox)
        item = service.get_item("movie", 101)
        self.assertTrue(item.local_only)
        self.assertTrue(service.set_watchlisted(item, True)["local_only"])
        self.assertTrue(service.mark_watched(item, watched_at=datetime.now(tz=UTC))["local_only"])
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=55, title_type="movie", title="Preview movie", tmdb_id=101),
            )
        mapped = service.get_item("movie", 101)
        self.assertEqual(mapped.trakt_id, 55)
        self.assertEqual(len(outbox.memberships), 1)
        self.assertEqual(len(outbox.histories), 1)
        self.assertEqual(outbox.memberships[0]["origin"], "tmdb_preview")
        self.assertIn("tmdb_preview_intent_id", outbox.memberships[0]["metadata"])
        with self.db.session() as session:
            self.assertTrue(all(row.status == "exported" for row in self.repo.list_intents(session)))

    def test_mapped_actions_stay_local_and_never_touch_trakt_in_tmdb_mode(self) -> None:
        class _LegacyServiceMustNotRun:
            calls = 0

            def __getattr__(self, _name):
                self.calls += 1
                raise AssertionError("TMDb mode must not invoke a legacy Trakt service")

        self.auth.config.catalog_provider_mode = "tmdb_preview"
        outbox = _FakeOutbox()
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=55, title_type="movie", title="Preview movie", tmdb_id=101),
            )
        legacy = _LegacyServiceMustNotRun()
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo, outbox)
        service.set_legacy_services(catalog=legacy, release_tracking=legacy, search_watch=legacy)
        item = service.get_item("movie", 101)

        self.assertIsNone(item.trakt_id)
        self.assertTrue(item.local_only)
        self.assertTrue(service.set_watchlisted(item, True)["local_only"])
        self.assertTrue(service.set_release_tracked(item, True)["local_only"])
        self.assertTrue(service.mark_watched(item, watched_at=datetime.now(tz=UTC))["local_only"])
        self.assertTrue(service.unwatch(item)["local_only"])
        self.assertTrue(service.set_rating(item, rating=8)["local_only"])
        self.assertEqual(legacy.calls, 0)
        self.assertEqual(outbox.memberships, [])
        self.assertEqual(outbox.histories, [])
        self.assertEqual(outbox.wake_count, 0)
        with self.db.session() as session:
            intents = self.repo.list_intents(session)
        self.assertTrue(intents)
        self.assertTrue(all(row.mapped_trakt_id is None for row in intents))
        self.assertTrue(all(row.status == "local_only" for row in intents))

    def test_confirmed_external_delivery_keeps_local_state(self) -> None:
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)
        item = service.get_item("movie", 101)
        service.mark_watched(item, watched_at=datetime.now(tz=UTC))
        with self.db.session() as session:
            intent = self.repo.intent(
                session,
                operation_type="history",
                title_type="movie",
                tmdb_id=101,
            )
            assert intent is not None
            intent_id = int(intent.id)
            revision = int(intent.revision)

        service.on_trakt_outbox_delivered(
            SimpleNamespace(
                origin="tmdb_preview",
                payload={
                    "tmdb_preview_intent_id": intent_id,
                    "tmdb_preview_revision": revision,
                },
            )
        )

        with self.db.session() as session:
            retained = self.repo.intent(
                session,
                operation_type="history",
                title_type="movie",
                tmdb_id=101,
            )
            self.assertIsNotNone(retained)
            self.assertEqual(retained.status, "exported")
            self.assertTrue(
                self.repo.effective_state(
                    session,
                    operation_type="history",
                    title_type="movie",
                    tmdb_id=101,
                )
            )

    def test_local_history_reads_use_bounded_query_count(self) -> None:
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)
        item = service.get_item("movie", 101)
        service.mark_watched(item, watched_at=datetime.now(tz=UTC))
        with self.db.session() as session:
            for tmdb_id in range(200, 220):
                self.repo.upsert_snapshot(
                    session,
                    {
                        "provider": "tmdb",
                        "title_type": "movie",
                        "tmdb_id": tmdb_id,
                        "title": f"Unwatched {tmdb_id}",
                    },
                )

        query_count = 0

        def count_query(*_args) -> None:
            nonlocal query_count
            query_count += 1

        event.listen(self.db._engine, "before_cursor_execute", count_query)
        try:
            rows = service.local_history_rows()
            row_queries = query_count
            query_count = 0
            titles = service.local_history_titles()
            title_queries = query_count
        finally:
            event.remove(self.db._engine, "before_cursor_execute", count_query)

        self.assertEqual(len(rows), 1)
        self.assertEqual(titles, ["Preview movie"])
        self.assertLessEqual(row_queries, 4)
        self.assertLessEqual(title_queries, 3)

    def test_tmdb_identity_uses_local_imdb_dataset_for_title_rating(self) -> None:
        service = TmdbCatalogService(
            self.db,
            self.auth,
            _FakeFactory(),
            self.titles,
            self.repo,
            imdb_client=_FakeImdbClient(),
        )

        item = service.get_item("movie", 101)

        self.assertEqual(item.imdb_id, "tt0133093")
        self.assertEqual(item.imdb_rating, 8.7)
        self.assertEqual(item.imdb_votes, 2_100_000)
        self.assertEqual(item.ratings_status, "ready")

    def test_unmapped_show_watch_panel_exposes_header_state_from_current_episodes(self) -> None:
        factory = _FakeFactory()
        factory.client.remote_seasons[(43125, 1)] = {
            "episodes": [
                {"episode_number": 1, "name": "Genesis", "air_date": "2011-10-14"},
                {"episode_number": 2, "name": "Survival of the Fittest", "air_date": "2011-10-21"},
            ],
        }
        service = TmdbCatalogService(
            self.db,
            self.auth,
            factory,
            self.titles,
            self.repo,
            imdb_client=_FakeImdbClient(),
        )
        item = service.get_item("show", 43125)
        service.mark_watched(item, watched_at=datetime.now(tz=UTC), season=1, episode=1)

        panel = service.load_watch_panel(43125, season=1)

        self.assertEqual(panel["title"], "Preview show")
        self.assertEqual(panel["tmdb_rating"], 8.1)
        self.assertEqual(panel["tmdb_votes"], 42)
        self.assertEqual(panel["imdb_rating"], 8.7)
        self.assertEqual(panel["imdb_votes"], 2_100_000)
        self.assertEqual(panel["ratings_status"], "ready")
        self.assertEqual(panel["watched_count"], 1)
        self.assertEqual(panel["released_count"], 2)
        self.assertEqual(panel["released_watched_count"], 1)
        self.assertTrue(panel["can_mark_title"])
        self.assertTrue(panel["can_unwatch_title"])
        self.assertTrue(panel["can_mark_season"])
        self.assertTrue(panel["can_unwatch_season"])

    def test_unmapped_show_watch_panel_prefers_current_tmdb_details_over_snapshot_rating(self) -> None:
        factory = _FakeFactory()
        with self.db.session() as session:
            self.repo.upsert_snapshot(
                session,
                {
                    "title_type": "show",
                    "tmdb_id": 43125,
                    "title": "Stale title",
                    "tmdb_rating": 5.0,
                    "tmdb_votes": 10,
                },
            )
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)

        panel = service.load_watch_panel(43125, season=1)

        self.assertEqual(panel["title"], "Preview show")
        self.assertEqual(panel["tmdb_rating"], 8.1)
        self.assertEqual(panel["tmdb_votes"], 42)

    def test_mapped_show_watch_panel_uses_tmdb_physical_seasons_in_tmdb_mode(self) -> None:
        class _LegacyWatchPanelMustNotRun:
            def load_show_panel(self, *_args, **_kwargs):
                raise AssertionError("TMDb mode must not use the legacy Trakt/IMDb panel coordinates")

        self.auth.config.catalog_provider_mode = "tmdb_preview"
        factory = _FakeFactory()
        factory.client.get_catalog_details = lambda _title_type, tmdb_id: {
            "id": int(tmdb_id),
            "name": "Re:ZERO",
            "overview": "",
            "vote_average": 8.0,
            "vote_count": 100,
            "seasons": [
                {"season_number": 1, "episode_count": 25},
                {"season_number": 2, "episode_count": 25},
            ],
            "external_ids": {"imdb_id": "tt5607616"},
        }
        factory.client.remote_seasons[(65942, 2)] = {
            "episodes": [
                {"episode_number": 17, "name": "A Journey Through Memories", "air_date": "2021-01-27"},
            ],
        }
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=77, title_type="show", title="Re:ZERO", tmdb_id=65942),
            )
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        service.set_legacy_services(search_watch=_LegacyWatchPanelMustNotRun())

        panel = service.load_watch_panel(65942, season=2)

        self.assertEqual(panel["selected_season"], 2)
        self.assertEqual([(item["season"], item["episode"]) for item in panel["episodes"]], [(2, 17)])
        self.assertIn((65942, 2), factory.client.remote_season_calls)

    def test_tmdb_history_enrichment_uses_physical_coordinates_for_episode_ratings(self) -> None:
        factory = _FakeFactory()
        factory.client.remote_seasons[(207468, 1)] = {
            "episodes": [
                {"episode_number": 20, "name": "Omen", "vote_average": 7.2, "vote_count": 11},
            ],
        }
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)

        rows = service.enrich_history_episode_ratings(
            [
                {
                    "provider": "trakt",
                    "tmdb_id": 207468,
                    "season": 1,
                    "episode": 20,
                    "episode_tmdb_rating": None,
                    "episode_tmdb_votes": None,
                    "episode_trakt_status": "ready",
                }
            ]
        )

        self.assertEqual(rows[0]["episode_tmdb_rating"], 7.2)
        self.assertEqual(rows[0]["episode_tmdb_votes"], 11)
        self.assertEqual(rows[0]["episode_trakt_status"], "ready")
        self.assertIn((207468, 1), factory.client.remote_season_calls)

    def test_mapped_tmdb_panel_never_falls_back_to_trakt_when_tmdb_is_unavailable(self) -> None:
        class _LegacyPanelMustNotRun:
            calls = 0

            def load_show_panel(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("TMDb mode must not open the legacy Trakt panel")

        self.auth.config.catalog_provider_mode = "tmdb_preview"
        factory = _FakeFactory()
        factory.client.get_catalog_details = lambda _title_type, tmdb_id: {
            "id": int(tmdb_id),
            "name": "Re:ZERO",
            "seasons": [{"season_number": 2, "episode_count": 25}],
            "external_ids": {"imdb_id": "tt5607616"},
        }

        def unavailable_season(*_args, **_kwargs):
            raise RuntimeError("TMDb unavailable")

        factory.client.get_catalog_season = unavailable_season
        legacy_panel = _LegacyPanelMustNotRun()
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=77, title_type="show", title="Re:ZERO", tmdb_id=65942),
            )
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        service.set_legacy_services(search_watch=legacy_panel)

        with self.assertRaisesRegex(RuntimeError, "TMDb unavailable"):
            service.load_watch_panel(65942, season=2)
        self.assertEqual(legacy_panel.calls, 0)

    def test_unmapped_show_scope_actions_update_each_episode_intent(self) -> None:
        factory = _FakeFactory()
        factory.client.remote_seasons[(43125, 1)] = {
            "episodes": [
                {"episode_number": 1, "name": "Genesis", "air_date": "2011-10-14"},
                {"episode_number": 2, "name": "Survival of the Fittest", "air_date": "2011-10-21"},
            ],
        }
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        item = service.get_item("show", 43125)

        marked = service.mark_show_scope_watched(item, watched_at=datetime.now(tz=UTC), season=1)
        marked_panel = service.load_watch_panel(43125, season=1)
        removed = service.unwatch_show_scope(item, season=1)
        removed_panel = service.load_watch_panel(43125, season=1)

        self.assertEqual(marked, 2)
        self.assertTrue(all(episode["watched"] for episode in marked_panel["episodes"]))
        self.assertFalse(marked_panel["can_mark_season"])
        self.assertTrue(marked_panel["can_unwatch_season"])
        self.assertEqual(removed, 2)
        self.assertTrue(all(not episode["watched"] for episode in removed_panel["episodes"]))
        self.assertTrue(removed_panel["can_mark_season"])
        self.assertFalse(removed_panel["can_unwatch_season"])

    def test_unmapped_episode_watch_projects_up_next_and_clears_release_tracking(self) -> None:
        factory = _FakeFactory()
        factory.client.get_catalog_details = lambda _title_type, tmdb_id: {
            "id": int(tmdb_id),
            "name": "Preview show",
            "first_air_date": "2011-10-14",
            "vote_average": 8.1,
            "vote_count": 42,
            "seasons": [{"season_number": 1, "episode_count": 2}],
            "external_ids": {"imdb_id": "tt0133093"},
        }
        factory.client.remote_seasons[(43125, 1)] = {
            "episodes": [
                {"episode_number": 1, "name": "Genesis", "air_date": "2011-10-14"},
                {
                    "episode_number": 2,
                    "name": "Survival of the Fittest",
                    "air_date": "2011-10-21",
                },
            ],
        }
        service = TmdbCatalogService(
            self.db,
            self.auth,
            factory,
            self.titles,
            self.repo,
            imdb_client=_FakeImdbClient(),
        )
        item = service.get_item("show", 43125)
        service.set_release_tracked(item, True)
        service.load_watch_panel(43125, season=1)

        result = service.mark_watched(
            item,
            watched_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            season=1,
            episode=1,
        )
        progress = service.local_progress_items()

        self.assertTrue(result["removed_from_release_tracking"])
        self.assertEqual(service.local_release_items(), [])
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0].tmdb_id, 43125)
        self.assertEqual(progress[0].completed, 1)
        self.assertEqual(progress[0].next_episode.season, 1)
        self.assertEqual(progress[0].next_episode.number, 2)
        self.assertEqual(progress[0].next_episode.title, "Survival of the Fittest")

    def test_unmapped_episode_rating_is_persisted_in_local_panel_and_progress(self) -> None:
        factory = _FakeFactory()
        factory.client.get_catalog_details = lambda _title_type, tmdb_id: {
            "id": int(tmdb_id),
            "name": "Preview show",
            "first_air_date": "2011-10-14",
            "vote_average": 8.1,
            "vote_count": 42,
            "seasons": [{"season_number": 1, "episode_count": 2}],
            "external_ids": {"imdb_id": "tt0133093"},
        }
        factory.client.remote_seasons[(43125, 1)] = {
            "episodes": [
                {"episode_number": 1, "name": "Genesis", "air_date": "2011-10-14"},
                {
                    "episode_number": 2,
                    "name": "Survival of the Fittest",
                    "air_date": "2011-10-21",
                },
            ],
        }
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        item = service.get_item("show", 43125)
        service.load_watch_panel(43125, season=1)
        service.mark_watched(
            item,
            watched_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            season=1,
            episode=1,
        )

        service.set_rating(item, rating=9, season=1, episode=1)
        panel = service.load_watch_panel(43125, season=1)
        progress = service.local_progress_items()
        history = service.local_history_rows()
        title_history = service.local_history_title_summaries()

        self.assertEqual(panel["episodes"][0]["user_rating"], 9)
        self.assertEqual(progress[0].title_episode_avg_rating, 9.0)
        self.assertEqual(history[0]["episode_title"], "Genesis")
        self.assertEqual(history[0]["display_rating"], 9)
        self.assertEqual(title_history[0]["title_key"], "show:tmdb:43125")
        self.assertEqual(title_history[0]["my_rating"], 9.0)
        self.assertEqual(service.local_show_episode_ratings(43125), {(1, 1): 9})

    def test_unmapped_watchlist_item_is_available_from_local_read_model(self) -> None:
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)
        item = service.get_item("movie", 101)

        service.set_watchlisted(item, True)
        saved = service.local_watchlist_items()

        self.assertEqual([(value.title_type, value.tmdb_id) for value in saved], [("movie", 101)])
        self.assertTrue(saved[0].is_watchlisted)

    def test_episode_schedule_overrides_stale_trakt_date_for_mapped_show(self) -> None:
        factory = _FakeFactory()
        factory.client.cached_seasons[(82684, 4)] = {
            "episodes": [
                {"episode_number": 17, "air_date": "2026-08-07"},
            ]
        }
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=135985,
                    title_type="show",
                    title="That Time I Got Reincarnated as a Slime",
                    tmdb_id=82684,
                ),
            )
        self.auth.config.catalog_provider_mode = "tmdb_preview"
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        item = ProgressSnapshot(
            trakt_id=135985,
            title="That Time I Got Reincarnated as a Slime",
            completed=16,
            aired=17,
            percent_completed=94.1,
            next_episode=EpisodeSummary(
                trakt_id=90017,
                season=4,
                number=17,
                title="Episode 17",
                first_aired=datetime(2026, 7, 31, 14, 0, tzinfo=UTC),
            ),
        )

        service.overlay_episode_air_dates([item])

        self.assertEqual(item.next_episode.first_aired, datetime(2026, 8, 7, 14, 0, tzinfo=UTC))
        self.assertEqual(factory.client.remote_season_calls, [])

    def test_progress_overlay_keeps_tmdb_rating_separate_from_imdb_display_coordinate(self) -> None:
        factory = _FakeFactory()
        factory.client.cached_seasons[(207468, 1)] = {
            "episodes": [
                {
                    "episode_number": 20,
                    "air_date": "2025-09-06",
                    "vote_average": 7.8,
                    "vote_count": 310,
                },
            ]
        }
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=197270,
                    title_type="show",
                    title="Kaiju No. 8",
                    tmdb_id=207468,
                    tmdb_rating=8.4,
                    tmdb_votes=772,
                ),
            )
        self.auth.config.catalog_provider_mode = "tmdb_preview"
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        item = ProgressSnapshot(
            trakt_id=197270,
            title="Kaiju No. 8",
            completed=19,
            aired=23,
            percent_completed=83.0,
            next_episode=EpisodeSummary(
                trakt_id=90020,
                season=1,
                number=20,
                title="Destiny",
                imdb_season=2,
                imdb_episode=8,
            ),
        )

        service.overlay_episode_air_dates([item])

        self.assertEqual(item.tmdb_id, 207468)
        self.assertEqual((item.title_tmdb_rating, item.title_tmdb_votes), (8.4, 772))
        self.assertEqual((item.next_episode.tmdb_rating, item.next_episode.tmdb_votes), (7.8, 310))
        self.assertEqual(item.next_episode.first_aired, datetime(2025, 9, 6, tzinfo=UTC))

    def test_episode_row_schedule_overrides_all_cached_dates_for_progress_count(self) -> None:
        factory = _FakeFactory()
        factory.client.cached_seasons[(82684, 4)] = {
            "episodes": [
                {"episode_number": 18, "air_date": "2026-08-14"},
                {"episode_number": 19, "air_date": "2026-08-21"},
            ]
        }
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=135985,
                    title_type="show",
                    title="That Time I Got Reincarnated as a Slime",
                    tmdb_id=82684,
                ),
            )
        self.auth.config.catalog_provider_mode = "tmdb_preview"
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        rows = [
            {
                "season": 4,
                "number": 18,
                "first_aired": datetime(2026, 8, 7, 14, 0, tzinfo=UTC),
            },
            {
                "season": 4,
                "number": 19,
                "first_aired": datetime(2026, 8, 14, 14, 0, tzinfo=UTC),
            },
        ]

        service.overlay_episode_row_air_dates(135985, rows)

        self.assertEqual(rows[0]["first_aired"], datetime(2026, 8, 14, 14, 0, tzinfo=UTC))
        self.assertEqual(rows[1]["first_aired"], datetime(2026, 8, 21, 14, 0, tzinfo=UTC))
        self.assertEqual(factory.client.remote_season_calls, [])

    def test_episode_schedule_remote_refresh_is_explicit(self) -> None:
        factory = _FakeFactory()
        factory.client.remote_seasons[(82684, 4)] = {
            "episodes": [{"episode_number": 17, "air_date": "2026-08-07"}],
        }
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(
                    trakt_id=135985,
                    title_type="show",
                    title="That Time I Got Reincarnated as a Slime",
                    tmdb_id=82684,
                ),
            )
        self.auth.config.catalog_provider_mode = "tmdb_preview"
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        item = ProgressSnapshot(
            trakt_id=135985,
            title="That Time I Got Reincarnated as a Slime",
            completed=16,
            aired=17,
            percent_completed=94.1,
            next_episode=EpisodeSummary(
                trakt_id=90017,
                season=4,
                number=17,
                title="Episode 17",
                first_aired=datetime(2026, 7, 31, 14, 0, tzinfo=UTC),
            ),
        )

        service.refresh_episode_air_dates([item])

        self.assertEqual(factory.client.remote_season_calls, [(82684, 4)])
        self.assertEqual(item.next_episode.first_aired, datetime(2026, 8, 7, 14, 0, tzinfo=UTC))

    def test_explore_exposes_real_popularity_and_weekly_trend_rank(self) -> None:
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)

        trending = service.explore_titles("movie", "trending", page=2)
        popular = service.explore_titles("movie", "popular", page=1)
        anticipated = service.explore_titles("movie", "anticipated", page=1)

        self.assertEqual(trending.items[0].popularity, 94.5)
        self.assertEqual(trending.items[0].explore_metric_kind, "weekly trend")
        self.assertEqual(getattr(trending.items[0], "explore_rank", None), 21)
        self.assertIsNone(trending.items[0].explore_metric_count)
        self.assertEqual(popular.items[0].explore_metric_kind, "popularity")
        self.assertEqual(popular.items[0].popularity, 94.5)
        self.assertIsNone(popular.items[0].explore_metric_count)
        self.assertEqual(anticipated.items[0].explore_metric_kind, "popularity")

    def test_explore_orders_each_feed_by_its_visible_metric(self) -> None:
        factory = _FakeFactory()
        payload = {
            "page": 1,
            "total_pages": 1,
            "results": [
                {"id": 301, "title": "Low", "popularity": 10.0, "vote_average": 8.0},
                {"id": 302, "title": "High", "popularity": 90.0, "vote_average": 8.0},
                {"id": 303, "title": "Middle", "popularity": 50.0, "vote_average": 8.0},
            ],
        }
        factory.client.trending_catalog = lambda _title_type, page=1: payload
        factory.client.discover_catalog = lambda _title_type, page=1, upcoming=False: payload
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)

        anticipated = service.explore_titles("movie", "anticipated", page=1)
        trending = service.explore_titles("movie", "trending", page=1)
        popular = service.explore_titles("movie", "popular", page=1)

        self.assertEqual([item.title for item in anticipated.items], ["High", "Middle", "Low"])
        self.assertEqual([item.title for item in trending.items], ["Low", "High", "Middle"])
        self.assertEqual([item.explore_rank for item in trending.items], [1, 2, 3])
        self.assertEqual([item.title for item in popular.items], ["High", "Middle", "Low"])

    def test_filtered_popular_sorts_candidates_collected_from_existing_pages(self) -> None:
        factory = _FakeFactory()
        calls: list[int] = []

        def discover_catalog(_title_type: str, *, page: int = 1, upcoming: bool = False) -> dict:
            calls.append(page)
            popularity = 10.0 if page == 1 else 90.0
            return {
                "page": page,
                "total_pages": 2,
                "results": [
                    {
                        "id": 400 + page,
                        "title": "Low" if page == 1 else "High",
                        "popularity": popularity,
                        "vote_average": 8.0,
                    }
                ],
            }

        factory.client.discover_catalog = discover_catalog
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)

        result = service.filtered_explore_titles(
            "show",
            "popular",
            page=1,
            limit=2,
            imdb_min=None,
            tmdb_min=0.0,
            max_scan_pages=2,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual([item.title for item in result.items], ["High", "Low"])

    def test_tmdb_explore_filters_projected_local_history_and_watchlist_without_legacy_service(self) -> None:
        class _LegacyServiceMustNotRun:
            def __getattr__(self, _name):
                raise AssertionError("TMDb mode must not read a legacy service")

        self.auth.config.catalog_provider_mode = "tmdb_preview"
        factory = _FakeFactory()
        factory.client.trending_catalog = lambda _title_type, *, page=1: {
            "page": page,
            "total_pages": 1,
            "results": [
                {"id": 101, "name": "Already watched", "first_air_date": "2025-01-01"},
                {"id": 102, "name": "Already watchlisted", "first_air_date": "2025-01-01"},
                {"id": 103, "name": "Fresh title", "first_air_date": "2025-01-01"},
            ],
        }
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=77, title_type="show", title="Already watched", tmdb_id=101),
            )
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=78, title_type="show", title="Already watchlisted", tmdb_id=102),
            )
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=79, title_type="show", title="Fresh title", tmdb_id=103),
            )
            session.add(
                HistoryEvent(
                    title_trakt_id=77,
                    title="Already watched",
                    title_type="show",
                    action="watched",
                    watched_at=datetime(2026, 8, 1),
                    season=1,
                    episode=1,
                    source="trakt",
                )
            )
            SyncStateRepository().set_value(
                session,
                "watchlist_snapshot_v1",
                '{"keys":[["show",78]],"items":[]}',
            )
        service = TmdbCatalogService(self.db, self.auth, factory, self.titles, self.repo)
        legacy = _LegacyServiceMustNotRun()
        service.set_legacy_services(catalog=legacy, release_tracking=legacy, search_watch=legacy)

        result = service.filtered_explore_titles(
            "show",
            "trending",
            page=1,
            limit=24,
            imdb_min=None,
            tmdb_min=None,
            max_scan_pages=1,
            hide_watchlisted=True,
            hide_history=True,
        )

        self.assertEqual([item.title for item in result.items], ["Fresh title"])
        with self.db.session() as session:
            history = self.repo.intent(
                session,
                operation_type="history",
                title_type="show",
                tmdb_id=101,
                season=1,
                episode=1,
            )
            watchlist = self.repo.intent(
                session,
                operation_type="watchlist",
                title_type="show",
                tmdb_id=102,
            )
            snapshot = self.repo.snapshot(session, "show", 101)
        self.assertIsNotNone(history)
        self.assertIsNotNone(watchlist)
        self.assertEqual(history.status, "local_only")
        self.assertIsNone(history.mapped_trakt_id)
        self.assertIsNone(snapshot.trakt_id)

    def test_tmdb_progress_projection_preserves_legacy_completion_boundary(self) -> None:
        self.auth.config.catalog_provider_mode = "tmdb_preview"
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=77, title_type="show", title="Completed locally", tmdb_id=101),
            )
            for number in (1, 2, 3):
                session.add(
                    EpisodeCache(
                        show_trakt_id=77,
                        episode_trakt_id=700 + number,
                        season=1,
                        number=number,
                        title=f"Episode {number}",
                        first_aired=datetime(2026, 1, number),
                    )
                )
            session.add(
                WatchProgress(
                    show_trakt_id=77,
                    show_title="Completed locally",
                    completed=2,
                    aired=2,
                    percent_completed=100.0,
                    next_episode_trakt_id=None,
                    next_episode_season=None,
                    next_episode_number=None,
                )
            )
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)

        self.assertEqual(service.local_progress_items(limit=None), [])

    def test_tmdb_projection_rebuilds_stale_flattened_seasons_from_imdb_coordinates(self) -> None:
        self.auth.config.catalog_provider_mode = "tmdb_preview"
        with self.db.session() as session:
            self.titles.upsert_title(
                session,
                TitleSummary(trakt_id=77, title_type="show", title="Re:ZERO", tmdb_id=65942),
            )
            for physical_number, imdb_season, imdb_episode in (
                (1, 1, 1),
                (2, 1, 2),
                (3, 2, 1),
            ):
                session.add(
                    EpisodeCache(
                        show_trakt_id=77,
                        episode_trakt_id=700 + physical_number,
                        season=1,
                        number=physical_number,
                        imdb_season=imdb_season,
                        imdb_episode=imdb_episode,
                        title=f"Episode {physical_number}",
                    )
                )
            session.add(
                WatchProgress(
                    show_trakt_id=77,
                    show_title="Re:ZERO",
                    completed=2,
                    aired=2,
                    percent_completed=100.0,
                    next_episode_season=1,
                    next_episode_number=3,
                    next_episode_title="Episode 3",
                )
            )
            self.repo.upsert_snapshot(
                session,
                {
                    "provider": "tmdb",
                    "title_type": "show",
                    "tmdb_id": 65942,
                    "trakt_id": None,
                    "title": "Re:ZERO",
                    "seasons": [{"season_number": 1, "episodes": [{"episode": 1}, {"episode": 2}, {"episode": 3}]}],
                    "local_progress": {"completed": 2, "aired": 2, "next_episode": {"season": 1, "episode": 3}},
                },
            )
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)

        item = service.get_item("show", 65942)

        self.assertEqual(
            [(season["season_number"], [episode["episode"] for episode in season["episodes"]]) for season in item.seasons],
            [(1, [1, 2]), (2, [1])],
        )
        self.assertEqual(item.trakt_id, None)

    def test_release_notifications_use_configured_delay_and_repeat(self) -> None:
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)
        sender = _FakeSender()
        service.set_notification_sender(sender)
        item = TmdbCatalogItem(
            title_type="movie",
            tmdb_id=202,
            title="Released preview",
            released_at=datetime.now(tz=UTC) - timedelta(days=1),
        )
        service.set_release_tracked(item, True)
        first = service.poll_releases(send_native=True, refresh_remote=False)
        second = service.poll_releases(send_native=True, refresh_remote=False)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(sender.messages, [("Released preview", "Movie is now available")])

    def test_notified_release_keeps_tmdb_date_when_legacy_mapping_disagrees(self) -> None:
        self.auth.config.movie_release_notification_delay_minutes = 25_080
        service = TmdbCatalogService(self.db, self.auth, _FakeFactory(), self.titles, self.repo)
        sender = _FakeSender()
        service.set_notification_sender(sender)
        now = datetime.now(tz=UTC)
        tmdb_release_at = now - timedelta(days=18)
        legacy_release_at = now - timedelta(days=15)
        service.set_release_tracked(
            TmdbCatalogItem(
                title_type="movie",
                tmdb_id=202,
                title="The Odyssey",
                released_at=tmdb_release_at,
            ),
            True,
        )
        service.set_legacy_services(
            release_tracking=SimpleNamespace(
                local_items=lambda: [
                    TitleSummary(
                        trakt_id=55,
                        title_type="movie",
                        title="The Odyssey",
                        tmdb_id=202,
                        released_at=legacy_release_at,
                    )
                ]
            )
        )

        self.assertEqual(len(service.poll_releases(send_native=True, refresh_remote=False)), 1)
        item = service.local_release_items()[0]

        self.assertEqual(item.released_at, tmdb_release_at)
        self.assertEqual(service.notified_release_keys(), {("movie", 202)})
        self.assertEqual(sender.messages, [("The Odyssey", "Movie is now available")])
        self.assertTrue(service.set_release_acknowledged("movie", 202, acknowledged=True))
        self.assertEqual(service.notified_release_keys(), set())
        self.assertTrue(service.set_release_acknowledged("movie", 202, acknowledged=False))
        self.assertEqual(service.notified_release_keys(), set())


if __name__ == "__main__":
    unittest.main()
