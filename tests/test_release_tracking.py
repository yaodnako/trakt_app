from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from trakt_tracker.application.release_tracking import ReleaseTrackingService
from trakt_tracker.config import AppConfig
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot, TitleSummary
from trakt_tracker.infrastructure.trakt.client import TraktClient, TraktError
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import (
    ProgressRepository,
    ReleaseTrackingRepository,
    TitleRepository,
    UserStateRepository,
)
from trakt_tracker.web.viewmodels import format_release_distance


class _Sender:
    def __init__(self) -> None:
        self.messages = []

    def send(self, message) -> None:
        self.messages.append(message)


def test_trakt_release_tracking_uses_existing_case_insensitive_list() -> None:
    client = TraktClient("id", "secret", "http://callback")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/users/me/lists":
            return [{"name": "RELEASE_TRACKING", "ids": {"trakt": 77}}]
        if path.endswith("/items") and method == "GET":
            return [{
                "type": "movie",
                "movie": {"title": "Arrival", "year": 2026, "released": "2026-07-01", "ids": {"trakt": 10}},
            }]
        return {"added": {"movies": 1}}

    client._request = request  # type: ignore[method-assign]
    items = client.get_release_tracking()
    client.set_release_tracking("movie", 10, tracked=True)

    assert items[0].is_release_tracked is True
    assert calls[1][1] == "/users/me/lists/77/items"
    assert calls[-1][1] == "/users/me/lists/77/items"
    assert calls[-1][2]["json"] == {"movies": [{"ids": {"trakt": 10}}]}


def test_trakt_release_tracking_missing_list_is_explicit() -> None:
    client = TraktClient("id", "secret", "http://callback")
    client._request = lambda *args, **kwargs: []  # type: ignore[method-assign]
    try:
        client.get_release_tracking()
    except TraktError as exc:
        assert "Release_tracking" in str(exc)
    else:
        raise AssertionError("Expected a missing-list error")


def test_release_tracking_acknowledgement_stops_repeats_and_badges_ignore_it() -> None:
    with TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.sqlite3")
        db.create_schema()
        release_repo = ReleaseTrackingRepository()
        progress_repo = ProgressRepository()
        release_at = datetime.now(tz=UTC) - timedelta(days=10)
        item = TitleSummary(1, "movie", "Released movie", released_at=release_at)
        client = SimpleNamespace(get_release_tracking=lambda: [item])
        auth = SimpleNamespace(get_client=lambda: client)
        config = AppConfig(movie_release_notification_delay_minutes=0, notification_repeat_minutes=1)
        sender = _Sender()
        service = ReleaseTrackingService(
            db,
            auth,
            SimpleNamespace(load=lambda: config),
            release_repo,
            progress_repo,
            sender,
            titles=TitleRepository(),
        )

        sent = service.poll()
        assert sent == [{"show_title": "Released movie", "message": "Movie is now available", "source": "release"}]
        local_items = service.local_items()
        assert [(item.title_type, item.trakt_id, item.is_release_tracked) for item in local_items] == [
            ("movie", 1, True)
        ]
        assert service.released_count() == 1
        assert service.set_acknowledged("movie", 1, acknowledged=True) is True
        assert service.poll() == []
        assert service.released_count() == 1
        db.close()


def test_progress_badge_counts_released_next_titles_not_seen_state() -> None:
    with TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.sqlite3")
        db.create_schema()
        progress_repo = ProgressRepository()
        with db.session() as session:
            progress_repo.upsert_progress(
                session,
                ProgressSnapshot(
                    trakt_id=9,
                    title="Show",
                    completed=1,
                    aired=2,
                    percent_completed=50,
                    next_episode=EpisodeSummary(
                        trakt_id=99,
                        season=1,
                        number=2,
                        title="Next",
                        first_aired=datetime.now(tz=UTC) - timedelta(hours=1),
                    ),
                ),
            )
        service = ReleaseTrackingService(
            db,
            SimpleNamespace(),
            SimpleNamespace(load=lambda: AppConfig()),
            ReleaseTrackingRepository(),
            progress_repo,
            _Sender(),
        )
        assert service.progress_waiting_count() == 1
        db.close()


def test_progress_badge_excludes_paused_and_dropped_titles() -> None:
    with TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.sqlite3")
        db.create_schema()
        progress_repo = ProgressRepository()
        states = UserStateRepository()
        next_episode = EpisodeSummary(
            trakt_id=99,
            season=1,
            number=2,
            title="Next",
            first_aired=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        with db.session() as session:
            for trakt_id, state in ((1, "active"), (2, "paused"), (3, "dropped")):
                progress_repo.upsert_progress(
                    session,
                    ProgressSnapshot(
                        trakt_id=trakt_id,
                        title=f"{state.title()} Show",
                        completed=1,
                        aired=2,
                        percent_completed=50,
                        next_episode=EpisodeSummary(
                            trakt_id=next_episode.trakt_id + trakt_id,
                            season=next_episode.season,
                            number=next_episode.number,
                            title=next_episode.title,
                            first_aired=next_episode.first_aired,
                        ),
                    ),
                )
                if state == "paused":
                    states.set_paused(session, trakt_id, True)
                elif state == "dropped":
                    states.set_archived(session, trakt_id, True)
        service = ReleaseTrackingService(
            db,
            SimpleNamespace(),
            SimpleNamespace(
                load=lambda: AppConfig(
                    show_dropped_in_progress=True,
                    show_paused_in_progress=True,
                )
            ),
            ReleaseTrackingRepository(),
            progress_repo,
            _Sender(),
        )

        assert service.progress_waiting_count() == 1
        db.close()


def test_release_notification_source_requires_unacknowledged_item_past_delay() -> None:
    with TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.sqlite3")
        db.create_schema()
        release_repo = ReleaseTrackingRepository()
        progress_repo = ProgressRepository()
        config = AppConfig(movie_release_notification_delay_minutes=120)
        service = ReleaseTrackingService(
            db,
            SimpleNamespace(),
            SimpleNamespace(load=lambda: config),
            release_repo,
            progress_repo,
            _Sender(),
        )
        with db.session() as session:
            release_repo.sync_items(
                session,
                [
                    TitleSummary(
                        1,
                        "movie",
                        "Released movie",
                        released_at=datetime.now(tz=UTC) - timedelta(minutes=30),
                    )
                ],
            )

        assert service.has_due_unacknowledged_release() is False
        with db.session() as session:
            row = release_repo.get(session, "movie", 1)
            row.release_at = (datetime.now(tz=UTC) - timedelta(minutes=121)).replace(tzinfo=None)
        assert service.has_due_unacknowledged_release() is True
        assert service.set_acknowledged("movie", 1, acknowledged=True) is True
        assert service.has_due_unacknowledged_release() is False
        db.close()


def test_release_distance_boundaries() -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    assert format_release_distance(now + timedelta(days=6), now=now) == "6 days"
    assert format_release_distance(now + timedelta(days=7), now=now) == "1.0 weeks"
    assert format_release_distance(now + timedelta(days=27), now=now) == "3.9 weeks"
    assert format_release_distance(now + timedelta(days=28), now=now) == "0.9 months"
    assert format_release_distance(now + timedelta(days=184), now=now) == "2027"
    assert format_release_distance(None, now=now) == "Release date unknown"
