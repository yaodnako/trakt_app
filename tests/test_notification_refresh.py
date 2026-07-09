from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from trakt_tracker.application.notification_refresh import NotificationRefreshWorkflow
from trakt_tracker.config import AppConfig
from trakt_tracker.domain import CalendarEntry, EpisodeSummary, ProgressSnapshot


class _FakeConfigStore:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def load(self) -> AppConfig:
        return self._config


class _FakeClient:
    def __init__(self, entries: list[CalendarEntry]) -> None:
        self._entries = entries

    def get_calendar(self, start_date: str, days: int) -> list[CalendarEntry]:
        return self._entries


class _FakeAuth:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def get_client(self) -> _FakeClient:
        return self._client


class _FakeNotificationRepository:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.tracked: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.logs: dict[tuple[int, int], object] = {}

    def get_log(self, session, show_trakt_id: int, episode_trakt_id: int):
        return self.logs.get((show_trakt_id, episode_trakt_id))

    def delete_sent(self, session, show_trakt_id: int, episode_trakt_id: int) -> None:
        self.deleted.append((show_trakt_id, episode_trakt_id))
        self.logs.pop((show_trakt_id, episode_trakt_id), None)

    def mark_sent(self, session, **kwargs) -> None:
        self.sent.append(kwargs)
        self.logs[(kwargs["show_trakt_id"], kwargs["episode_trakt_id"])] = type(
            "FakeLog",
            (),
            {
                "sent_at": datetime.now(tz=UTC),
                "last_sent_at": datetime.now(tz=UTC),
                "seen_at": None,
                "notify_count": 1,
            },
        )()

    def track_released(self, session, **kwargs) -> None:
        self.tracked.append(kwargs)
        self.logs[(kwargs["show_trakt_id"], kwargs["episode_trakt_id"])] = type(
            "FakeLog",
            (),
            {
                "sent_at": kwargs["released_at"],
                "last_sent_at": kwargs["released_at"],
                "seen_at": None,
                "notify_count": 0,
            },
        )()


class _FakeProgressRepository:
    def __init__(self, next_episode: EpisodeSummary) -> None:
        self._next_episode = next_episode

    def list_in_progress(self, session, dropped_only: bool = False) -> list[ProgressSnapshot]:
        return [
            ProgressSnapshot(
                trakt_id=1,
                title="Tracked Show",
                completed=0,
                aired=1,
                percent_completed=0.0,
                next_episode=self._next_episode,
            )
        ]


class _FakeSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, message) -> None:
        self.messages.append((message.title, message.body))


class NotificationRefreshWorkflowTests(unittest.TestCase):
    def _poll(self, *, first_aired: datetime, release_delay_minutes: int = 120) -> tuple[list[dict], _FakeSender, _FakeNotificationRepository]:
        episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Translated Later",
            first_aired=first_aired,
        )
        entry = CalendarEntry(show_trakt_id=1, show_title="Tracked Show", episode=episode)
        return self._poll_entry(entry=entry, progress_episode=episode, release_delay_minutes=release_delay_minutes)

    def _poll_entry(
        self,
        *,
        entry: CalendarEntry,
        progress_episode: EpisodeSummary,
        release_delay_minutes: int = 120,
    ) -> tuple[list[dict], _FakeSender, _FakeNotificationRepository]:
        config = AppConfig(notification_release_delay_minutes=release_delay_minutes)
        notification_repo = _FakeNotificationRepository()
        sender = _FakeSender()
        workflow = NotificationRefreshWorkflow(
            db=type("FakeDB", (), {"session": lambda self: nullcontext(object())})(),
            auth_service=_FakeAuth(_FakeClient([entry])),
            config_store=_FakeConfigStore(config),
            notification_repo=notification_repo,
            episode_repo=object(),
            progress_repo=_FakeProgressRepository(progress_episode),
            sender=sender,
        )
        return workflow.poll_upcoming(send_native=True), sender, notification_repo

    def test_poll_upcoming_skips_recent_release_inside_configured_delay(self) -> None:
        sent, sender, notification_repo = self._poll(first_aired=datetime.now(tz=UTC) - timedelta(minutes=30))

        self.assertEqual(sent, [])
        self.assertEqual(sender.messages, [])
        self.assertEqual(notification_repo.sent, [])
        self.assertEqual(len(notification_repo.tracked), 1)

    def test_poll_upcoming_sends_after_configured_release_delay(self) -> None:
        sent, sender, notification_repo = self._poll(first_aired=datetime.now(tz=UTC) - timedelta(minutes=121))

        self.assertEqual(sent, [{"show_title": "Tracked Show", "message": "S01E02 Translated Later"}])
        self.assertEqual(sender.messages, [("Tracked Show", "S01E02 Translated Later")])
        self.assertEqual(len(notification_repo.sent), 1)

    def test_poll_upcoming_uses_progress_air_date_when_calendar_omits_it(self) -> None:
        progress_episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Translated Later",
            first_aired=datetime.now(tz=UTC) - timedelta(minutes=121),
        )
        calendar_episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Translated Later",
            first_aired=None,
        )
        entry = CalendarEntry(show_trakt_id=1, show_title="Tracked Show", episode=calendar_episode)

        sent, sender, notification_repo = self._poll_entry(entry=entry, progress_episode=progress_episode)

        self.assertEqual(sent, [{"show_title": "Tracked Show", "message": "S01E02 Translated Later"}])
        self.assertEqual(sender.messages, [("Tracked Show", "S01E02 Translated Later")])
        self.assertEqual(len(notification_repo.sent), 1)


if __name__ == "__main__":
    unittest.main()
