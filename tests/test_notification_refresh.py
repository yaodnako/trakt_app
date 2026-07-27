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
        self.seen: list[dict] = []
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
                "show_trakt_id": kwargs["show_trakt_id"],
                "show_title": kwargs["show_title"],
                "episode_trakt_id": kwargs["episode_trakt_id"],
                "season": kwargs["season"],
                "episode": kwargs["episode"],
                "message": kwargs["message"],
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
                "show_trakt_id": kwargs["show_trakt_id"],
                "show_title": kwargs["show_title"],
                "episode_trakt_id": kwargs["episode_trakt_id"],
                "season": kwargs["season"],
                "episode": kwargs["episode"],
                "message": kwargs["message"],
                "sent_at": kwargs["released_at"],
                "last_sent_at": kwargs["released_at"],
                "seen_at": None,
                "notify_count": 0,
            },
        )()

    def list_unseen(self, session) -> list[object]:
        return [log for log in self.logs.values() if log.seen_at is None]

    def mark_seen(self, session, **kwargs) -> None:
        self.seen.append(kwargs)
        key = (kwargs["show_trakt_id"], kwargs["episode_trakt_id"])
        log = self.logs.get(key)
        if log is None:
            self.logs[key] = type(
                "FakeLog",
                (),
                {
                    "show_trakt_id": kwargs["show_trakt_id"],
                    "show_title": kwargs["show_title"],
                    "episode_trakt_id": kwargs["episode_trakt_id"],
                    "season": kwargs["season"],
                    "episode": kwargs["episode"],
                    "message": kwargs["message"],
                    "sent_at": datetime.now(tz=UTC),
                    "last_sent_at": datetime.now(tz=UTC),
                    "seen_at": datetime.now(tz=UTC),
                    "notify_count": 0,
                },
            )()
            return
        log.seen_at = datetime.now(tz=UTC)


class _FakeProgressRepository:
    def __init__(self, next_episode: EpisodeSummary, *, paused: bool = False) -> None:
        self._next_episode = next_episode
        self._paused = paused
        self.requested_limits: list[int | None | str] = []

    def list_in_progress(self, session, *, view="active", **kwargs) -> list[ProgressSnapshot]:
        self.requested_limits.append(kwargs.get("limit", "missing"))
        normalized_view = getattr(view, "value", view)
        expected_view = "paused" if self._paused else "active"
        if normalized_view != expected_view:
            return []
        return [
            ProgressSnapshot(
                trakt_id=1,
                title="Tracked Show",
                completed=0,
                aired=1,
                percent_completed=0.0,
                next_episode=self._next_episode,
                is_paused=self._paused,
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
        paused: bool = False,
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
            progress_repo=_FakeProgressRepository(progress_episode, paused=paused),
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

        self.assertEqual(
            sent,
            [{"show_title": "Tracked Show", "message": "S01E02 Translated Later", "source": "progress"}],
        )
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

        self.assertEqual(
            sent,
            [{"show_title": "Tracked Show", "message": "S01E02 Translated Later", "source": "progress"}],
        )
        self.assertEqual(sender.messages, [("Tracked Show", "S01E02 Translated Later")])
        self.assertEqual(len(notification_repo.sent), 1)

    def test_poll_upcoming_marks_paused_release_seen_without_notification(self) -> None:
        episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Paused Episode",
            first_aired=datetime.now(tz=UTC) - timedelta(minutes=121),
        )
        entry = CalendarEntry(show_trakt_id=1, show_title="Tracked Show", episode=episode)

        sent, sender, notification_repo = self._poll_entry(
            entry=entry,
            progress_episode=episode,
            paused=True,
        )

        self.assertEqual(sent, [])
        self.assertEqual(sender.messages, [])
        self.assertEqual(len(notification_repo.seen), 1)
        self.assertIsNotNone(notification_repo.logs[(1, 70)].seen_at)

    def test_notification_matching_requests_unlimited_progress_rows(self) -> None:
        episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Current",
            first_aired=datetime.now(tz=UTC) - timedelta(minutes=121),
        )
        entry = CalendarEntry(show_trakt_id=1, show_title="Tracked Show", episode=episode)
        progress_repo = _FakeProgressRepository(episode)
        workflow = NotificationRefreshWorkflow(
            db=type("FakeDB", (), {"session": lambda self: nullcontext(object())})(),
            auth_service=_FakeAuth(_FakeClient([entry])),
            config_store=_FakeConfigStore(AppConfig(notification_release_delay_minutes=120)),
            notification_repo=_FakeNotificationRepository(),
            episode_repo=object(),
            progress_repo=progress_repo,
            sender=_FakeSender(),
        )

        workflow.poll_upcoming(send_native=False)

        self.assertEqual(progress_repo.requested_limits, [None, None])

    def test_poll_upcoming_repeats_unseen_current_episode_after_calendar_window(self) -> None:
        released_at = datetime.now(tz=UTC) - timedelta(days=30)
        episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Still New",
            first_aired=released_at,
        )
        config = AppConfig(
            notification_release_delay_minutes=120,
            notification_repeat_minutes=60,
        )
        notification_repo = _FakeNotificationRepository()
        notification_repo.track_released(
            None,
            show_trakt_id=1,
            show_title="Tracked Show",
            episode_trakt_id=70,
            season=1,
            episode=2,
            message="S01E02 Still New",
            released_at=released_at,
        )
        sender = _FakeSender()
        workflow = NotificationRefreshWorkflow(
            db=type("FakeDB", (), {"session": lambda self: nullcontext(object())})(),
            auth_service=_FakeAuth(_FakeClient([])),
            config_store=_FakeConfigStore(config),
            notification_repo=notification_repo,
            episode_repo=object(),
            progress_repo=_FakeProgressRepository(episode),
            sender=sender,
        )

        sent = workflow.poll_upcoming(send_native=True)

        self.assertEqual(
            sent,
            [{"show_title": "Tracked Show", "message": "S01E02 Still New", "source": "progress"}],
        )
        self.assertEqual(sender.messages, [("Tracked Show", "S01E02 Still New")])

    def test_poll_upcoming_drops_unseen_episode_that_is_no_longer_up_next(self) -> None:
        released_at = datetime.now(tz=UTC) - timedelta(days=30)
        stale_episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Old Next",
            first_aired=released_at,
        )
        current_episode = EpisodeSummary(
            trakt_id=71,
            season=1,
            number=3,
            title="Current Next",
            first_aired=released_at,
        )
        notification_repo = _FakeNotificationRepository()
        notification_repo.track_released(
            None,
            show_trakt_id=1,
            show_title="Tracked Show",
            episode_trakt_id=stale_episode.trakt_id,
            season=stale_episode.season,
            episode=stale_episode.number,
            message="S01E02 Old Next",
            released_at=released_at,
        )
        sender = _FakeSender()
        workflow = NotificationRefreshWorkflow(
            db=type("FakeDB", (), {"session": lambda self: nullcontext(object())})(),
            auth_service=_FakeAuth(_FakeClient([])),
            config_store=_FakeConfigStore(AppConfig()),
            notification_repo=notification_repo,
            episode_repo=object(),
            progress_repo=_FakeProgressRepository(current_episode),
            sender=sender,
        )

        sent = workflow.poll_upcoming(send_native=True)

        self.assertEqual(sent, [])
        self.assertEqual(sender.messages, [])
        self.assertEqual(notification_repo.deleted, [(1, 70)])

    def test_pending_progress_source_requires_new_episode_and_elapsed_delay(self) -> None:
        recent_episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Recent",
            first_aired=datetime.now(tz=UTC) - timedelta(minutes=30),
        )
        notification_repo = _FakeNotificationRepository()
        notification_repo.track_released(
            None,
            show_trakt_id=1,
            show_title="Tracked Show",
            episode_trakt_id=70,
            season=1,
            episode=2,
            message="S01E02 Recent",
            released_at=recent_episode.first_aired,
        )
        workflow = NotificationRefreshWorkflow(
            db=type("FakeDB", (), {"session": lambda self: nullcontext(object())})(),
            auth_service=_FakeAuth(_FakeClient([])),
            config_store=_FakeConfigStore(AppConfig(notification_release_delay_minutes=120)),
            notification_repo=notification_repo,
            episode_repo=object(),
            progress_repo=_FakeProgressRepository(recent_episode),
            sender=_FakeSender(),
        )

        self.assertFalse(workflow.has_due_unseen_current_episode())
        notification_repo.logs[(1, 70)].sent_at = datetime.now(tz=UTC) - timedelta(minutes=121)
        recent_episode.first_aired = datetime.now(tz=UTC) - timedelta(minutes=121)
        self.assertTrue(workflow.has_due_unseen_current_episode())

    def test_pending_progress_source_ignores_paused_episode(self) -> None:
        episode = EpisodeSummary(
            trakt_id=70,
            season=1,
            number=2,
            title="Paused",
            first_aired=datetime.now(tz=UTC) - timedelta(minutes=121),
        )
        notification_repo = _FakeNotificationRepository()
        notification_repo.track_released(
            None,
            show_trakt_id=1,
            show_title="Tracked Show",
            episode_trakt_id=70,
            season=1,
            episode=2,
            message="S01E02 Paused",
            released_at=episode.first_aired,
        )
        workflow = NotificationRefreshWorkflow(
            db=type("FakeDB", (), {"session": lambda self: nullcontext(object())})(),
            auth_service=_FakeAuth(_FakeClient([])),
            config_store=_FakeConfigStore(AppConfig(notification_release_delay_minutes=120)),
            notification_repo=notification_repo,
            episode_repo=object(),
            progress_repo=_FakeProgressRepository(episode, paused=True),
            sender=_FakeSender(),
        )

        self.assertFalse(workflow.has_due_unseen_current_episode())


if __name__ == "__main__":
    unittest.main()
