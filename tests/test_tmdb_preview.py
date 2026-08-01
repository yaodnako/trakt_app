from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from trakt_tracker.application.tmdb_catalog import TmdbCatalogItem, TmdbCatalogService
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.domain import TitleSummary
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import TitleRepository
from trakt_tracker.persistence.tmdb_preview import TmdbPreviewRepository


class _FakeTmdbClient:
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

    def get_catalog_season(self, _tmdb_id: int, _season: int) -> dict:
        return {"episodes": []}

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
        path.write_text('{"client_id":"old"}', encoding="utf-8")
        store = ConfigStore(path)
        self.assertEqual(store.load().catalog_provider_mode, "trakt")
        config = AppConfig(catalog_provider_mode="not-a-provider")
        store.save(config)
        self.assertEqual(store.load().catalog_provider_mode, "trakt")

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


if __name__ == "__main__":
    unittest.main()
