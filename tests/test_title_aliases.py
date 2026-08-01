from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from trakt_tracker.application.title_aliases import TitleAliasService
from trakt_tracker.infrastructure.trakt.client import TraktClient
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import HistoryRepository, TitleAliasRepository


class _FakeTraktClient:
    def __init__(self, translations: dict[tuple[str, int], list[dict]]) -> None:
        self.translations = translations
        self.calls: list[tuple[int, str, str]] = []

    def get_title_translations(self, trakt_id: int, title_type: str, language: str) -> list[dict]:
        self.calls.append((trakt_id, title_type, language))
        return list(self.translations.get((title_type, trakt_id), []))


class _FakeAuth:
    def __init__(self, client: _FakeTraktClient) -> None:
        self.client = client

    def is_authorized(self) -> bool:
        return True

    def get_client(self) -> _FakeTraktClient:
        return self.client


class TitleAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmpdir.name) / "test.sqlite3")
        self.db.create_schema()
        self.history = HistoryRepository()
        self.aliases = TitleAliasRepository()

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def test_cached_russian_alias_matches_history_without_second_request(self) -> None:
        now = datetime(2026, 7, 22, tzinfo=UTC)
        with self.db.session() as session:
            self.history.add_event(
                session,
                trakt_history_id=1,
                title_trakt_id=123,
                title="Frieren: Beyond Journey's End",
                title_type="show",
                action="watched",
                watched_at=now,
                season=1,
                episode=1,
            )
        client = _FakeTraktClient(
            {("show", 123): [{"title": "Провожающая в последний путь Фрирен", "language": "ru"}]}
        )
        service = TitleAliasService(self.db, _FakeAuth(client), self.aliases)

        result = service.refresh_due_history_titles(now=now)
        second_result = service.refresh_due_history_titles(now=now + timedelta(days=1))

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.ready, 1)
        self.assertEqual(second_result.attempted, 0)
        self.assertEqual(client.calls, [(123, "show", "ru")])
        with self.db.session() as session:
            rows = self.history.list_filtered(session, title_filter="фрирен")
            uppercase_rows = self.history.list_filtered(session, title_filter="ПРОВОЖАЮЩАЯ")
        self.assertEqual([row.title_trakt_id for row in rows], [123])
        self.assertEqual([row.title_trakt_id for row in uppercase_rows], [123])

    def test_confirmed_missing_translation_is_not_retried_until_due(self) -> None:
        now = datetime(2026, 7, 22, tzinfo=UTC)
        with self.db.session() as session:
            self.history.add_event(
                session,
                trakt_history_id=2,
                title_trakt_id=456,
                title="Brand New Show",
                title_type="show",
                action="watched",
                watched_at=now,
                season=1,
                episode=1,
            )
        client = _FakeTraktClient({})
        service = TitleAliasService(self.db, _FakeAuth(client), self.aliases)

        service.refresh_due_history_titles(now=now)
        service.refresh_due_history_titles(now=now + timedelta(days=29))
        due_result = service.refresh_due_history_titles(now=now + timedelta(days=30))

        self.assertEqual(due_result.attempted, 1)
        self.assertEqual(client.calls, [(456, "show", "ru"), (456, "show", "ru")])

    def test_trakt_client_uses_cached_title_translation_endpoint(self) -> None:
        client = TraktClient("client-id", "client-secret", "http://127.0.0.1/callback")
        request = Mock(return_value=[{"title": "Дюна", "language": "ru"}])
        client._request = request
        self.addCleanup(client.close)

        translations = client.get_title_translations(11, "movie", "RU")

        self.assertEqual(translations, [{"title": "Дюна", "language": "ru"}])
        request.assert_called_once_with("GET", "/movies/11/translations/ru")


if __name__ == "__main__":
    unittest.main()
