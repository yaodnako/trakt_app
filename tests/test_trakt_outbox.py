from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from trakt_tracker.application.catalog import CatalogService
from trakt_tracker.application.history import HistoryService
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.release_tracking import ReleaseTrackingService
from trakt_tracker.application.trakt_outbox import TraktOutboxService
from trakt_tracker.domain import EpisodeSummary, HistoryItemInput, RatingInput
from trakt_tracker.infrastructure.cache import ProviderCache
from trakt_tracker.infrastructure.trakt.client import (
    TraktClient,
    TraktMutationUncertain,
    TraktRateLimitError,
    TraktRequestError,
)
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.models import HistoryEvent, WatchProgress
from trakt_tracker.persistence.repositories import (
    EpisodeRepository,
    HistoryRepository,
    ProgressRepository,
    ReleaseTrackingRepository,
    SyncStateRepository,
    TitleRepository,
    UserStateRepository,
)
from trakt_tracker.persistence.trakt_outbox import (
    OUTBOX_PENDING,
    OUTBOX_SENDING,
    OUTBOX_UNCERTAIN,
    TraktOutboxRepository,
)


class _Auth:
    def __init__(self, client=None, *, authorized: bool = True) -> None:
        self.client = client
        self.authorized = authorized
        self.config = SimpleNamespace(tmdb_api_key="", tmdb_read_access_token="")

    def is_authorized(self) -> bool:
        return self.authorized

    def get_client(self):
        if self.client is None:
            raise AssertionError("A local mutation attempted to use Trakt")
        return self.client


class _DeliveryClient:
    def __init__(self) -> None:
        self.history_posts: list[list[HistoryItemInput]] = []
        self.remote_history: list[dict] = []
        self.history_error: Exception | None = None
        self.watchlist_error: Exception | None = None
        self.watchlist_writes: list[tuple[str, int, bool]] = []

    def add_history_items(self, items: list[HistoryItemInput]) -> dict:
        self.history_posts.append(list(items))
        if self.history_error is not None:
            raise self.history_error
        return {"added": {"movies": len(items)}, "not_found": {"movies": []}}

    def remove_history_items(self, items: list[HistoryItemInput]) -> dict:
        self.history_posts.append(list(items))
        if self.history_error is not None:
            raise self.history_error
        return {"deleted": {"movies": len(items)}, "not_found": {"movies": []}}

    def get_watch_history_page(self, title_type=None, limit=1000, page=1):
        assert title_type in {"movie", "show"}
        return list(self.remote_history), {"X-Pagination-Page-Count": "1"}

    def set_watchlist(self, title_type: str, trakt_id: int, *, watchlisted: bool) -> dict:
        self.watchlist_writes.append((title_type, trakt_id, watchlisted))
        if self.watchlist_error is not None:
            raise self.watchlist_error
        return {"added": {"movies": 1}, "not_found": {"movies": []}}

    def get_watchlist(self):
        return []

    def get_release_tracking(self):
        return []

    def get_paused_shows(self, *, limit: int, page: int):
        return []

    def get_dropped_shows(self, *, limit: int, page: int):
        return []


def _database(path: Path) -> Database:
    db = Database(path)
    db.create_schema()
    return db


def _outbox_service(db: Database, auth: _Auth) -> tuple[TraktOutboxService, TraktOutboxRepository]:
    repository = TraktOutboxRepository()
    return (
        TraktOutboxService(
            db,
            auth,
            repository,
            EpisodeRepository(),
            SyncStateRepository(),
        ),
        repository,
    )


def _enqueue_movie_watch(service: TraktOutboxService, db: Database, trakt_id: int, watched_at: datetime) -> str:
    with db.session() as session:
        key = service.enqueue_history(
            session,
            title_type="movie",
            trakt_id=trakt_id,
            title=f"Movie {trakt_id}",
            desired_watched=True,
            base_watched=False,
            watched_at=watched_at,
        )
    assert key is not None
    return key


def test_outbox_survives_restart_and_expired_lease_becomes_uncertain(tmp_path: Path) -> None:
    path = tmp_path / "profile.sqlite3"
    db = _database(path)
    repository = TraktOutboxRepository()
    now = datetime.utcnow()
    with db.session() as session:
        repository.enqueue(
            session,
            operation_key="watchlist:movie:1",
            operation_type="watchlist",
            base_state={"member": False},
            desired_state={"member": True},
            payload={"title_type": "movie", "trakt_id": 1},
        )
    with db.session() as session:
        claimed = repository.claim_due(session, now=now, lease_seconds=10)
        assert len(claimed) == 1
    db.close()

    reopened = _database(path)
    try:
        with reopened.session() as session:
            row = repository.get(session, "watchlist:movie:1")
            assert row is not None
            assert row.status == OUTBOX_SENDING
            assert repository.recover_expired_leases(session, now=now + timedelta(seconds=11)) == 1
        with reopened.session() as session:
            row = repository.get(session, "watchlist:movie:1")
            assert row is not None
            assert row.status == OUTBOX_UNCERTAIN
            assert row.lease_token is None
    finally:
        reopened.close()


def test_local_state_and_outbox_roll_back_together(tmp_path: Path) -> None:
    db = _database(tmp_path / "atomic.sqlite3")
    repository = TraktOutboxRepository()
    titles = TitleRepository()
    try:
        with pytest.raises(RuntimeError, match="abort transaction"):
            with db.session() as session:
                titles.upsert_title(
                    session,
                    SimpleNamespace(
                        trakt_id=7,
                        title_type="movie",
                        title="Atomic",
                        year=None,
                        overview="",
                        poster_url="",
                        backdrop_url="",
                        status="",
                        slug="",
                        trakt_rating=None,
                        trakt_votes=None,
                        tmdb_id=None,
                        tmdb_rating=None,
                        tmdb_votes=None,
                        imdb_id="",
                        imdb_rating=None,
                        imdb_votes=None,
                        ratings_status="unknown",
                        ratings_refreshed_at=None,
                        poster_status="unknown",
                        poster_refreshed_at=None,
                        backdrop_status="unknown",
                        backdrop_refreshed_at=None,
                    ),
                )
                repository.enqueue(
                    session,
                    operation_key="watchlist:movie:7",
                    operation_type="watchlist",
                    base_state={"member": False},
                    desired_state={"member": True},
                    payload={"title_type": "movie", "trakt_id": 7},
                )
                raise RuntimeError("abort transaction")
        with db.session() as session:
            assert titles.get_title(session, 7) is None
            assert repository.list_items(session) == []
    finally:
        db.close()


def test_coalescing_revision_change_and_dependency_order(tmp_path: Path) -> None:
    db = _database(tmp_path / "coalescing.sqlite3")
    repository = TraktOutboxRepository()
    now = datetime.utcnow()
    try:
        with db.session() as session:
            repository.enqueue(
                session,
                operation_key="watchlist:movie:1",
                operation_type="watchlist",
                base_state={"member": False},
                desired_state={"member": True},
                payload={"title_type": "movie", "trakt_id": 1},
            )
            repository.enqueue(
                session,
                operation_key="watchlist:movie:1",
                operation_type="watchlist",
                base_state={"member": False},
                desired_state={"member": False},
                payload={"title_type": "movie", "trakt_id": 1},
            )
            assert repository.get(session, "watchlist:movie:1") is None

            repository.enqueue(
                session,
                operation_key="history:movie:2",
                operation_type="history",
                base_state={"watched": False},
                desired_state={"watched": True},
                payload={"title_type": "movie", "trakt_id": 2},
            )
            repository.enqueue(
                session,
                operation_key="watchlist:movie:2",
                operation_type="watchlist",
                base_state={"member": True},
                desired_state={"member": False},
                payload={"title_type": "movie", "trakt_id": 2},
                dependency_key="history:movie:2",
            )
        with db.session() as session:
            first = repository.claim_due(session, now=now + timedelta(seconds=1))
            assert [row.operation_key for row in first] == ["history:movie:2"]
        with db.session() as session:
            repository.enqueue(
                session,
                operation_key="history:movie:2",
                operation_type="history",
                base_state={"watched": False},
                desired_state={"watched": False},
                payload={"title_type": "movie", "trakt_id": 2},
            )
            changed = repository.get(session, "history:movie:2")
            assert changed is not None and changed.revision == 2
            assert changed.status == OUTBOX_SENDING
        with db.session() as session:
            assert repository.complete(session, first[0]) is False
            changed = repository.get(session, "history:movie:2")
            assert changed is not None and changed.status == OUTBOX_UNCERTAIN
        with db.session() as session:
            repository.discard_blocked(session, -1)
            changed = repository.get(session, "history:movie:2")
            assert changed is not None
            session.delete(changed)
        with db.session() as session:
            second = repository.claim_due(session, now=now + timedelta(seconds=2))
            assert [row.operation_key for row in second] == ["watchlist:movie:2"]
    finally:
        db.close()


def test_scoped_dependency_waits_for_every_history_operation(tmp_path: Path) -> None:
    db = _database(tmp_path / "scoped-dependency.sqlite3")
    repository = TraktOutboxRepository()
    now = datetime.utcnow()
    try:
        with db.session() as session:
            for episode in (1, 2):
                repository.enqueue(
                    session,
                    operation_key=f"history:show:5:s1:e{episode}",
                    operation_type="history",
                    base_state={"watched": False},
                    desired_state={"watched": True},
                    payload={"title_type": "show", "trakt_id": 5, "season": 1, "episode": episode},
                )
            repository.enqueue(
                session,
                operation_key="watchlist:show:5",
                operation_type="watchlist",
                base_state={"member": True},
                desired_state={"member": False},
                payload={"title_type": "show", "trakt_id": 5},
                dependency_key="scope:history:show:5",
            )
        with db.session() as session:
            history_claims = repository.claim_due(session, now=now + timedelta(seconds=1))
            assert len(history_claims) == 2
        with db.session() as session:
            assert repository.complete(session, history_claims[0]) is True
        with db.session() as session:
            assert repository.claim_due(session, now=now + timedelta(seconds=2)) == []
            assert repository.complete(session, history_claims[1]) is True
        with db.session() as session:
            follow_up = repository.claim_due(session, now=now + timedelta(seconds=3))
            assert [item.operation_key for item in follow_up] == ["watchlist:show:5"]
    finally:
        db.close()


def test_history_batch_and_uncertain_reconcile_avoid_duplicate_post(tmp_path: Path) -> None:
    db = _database(tmp_path / "delivery.sqlite3")
    client = _DeliveryClient()
    service, repository = _outbox_service(db, _Auth(client))
    watched_at = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    try:
        _enqueue_movie_watch(service, db, 1, watched_at)
        _enqueue_movie_watch(service, db, 2, watched_at)
        result = service.drain()
        assert result.delivered == 2
        assert len(client.history_posts) == 1
        assert len(client.history_posts[0]) == 2

        _enqueue_movie_watch(service, db, 3, watched_at)
        client.history_error = TraktMutationUncertain("connection ended after upload")
        result = service.drain()
        assert result.delivered == 0
        assert len(client.history_posts) == 2
        with db.session() as session:
            row = repository.get(session, "history:movie:3")
            assert row is not None and row.status == OUTBOX_UNCERTAIN

        client.history_error = None
        client.remote_history = [
            {
                "watched_at": "2026-07-31T12:00:00Z",
                "movie": {"ids": {"trakt": 3}},
            }
        ]
        service.retry()
        result = service.drain()
        assert result.delivered == 1
        assert len(client.history_posts) == 2
        with db.session() as session:
            assert repository.get(session, "history:movie:3") is None
    finally:
        db.close()


def test_history_batch_refreshes_progress_once_per_show(tmp_path: Path) -> None:
    db = _database(tmp_path / "history-progress-refresh.sqlite3")
    client = _DeliveryClient()
    service, _repository = _outbox_service(db, _Auth(client))
    refreshed: list[int] = []
    service.set_history_delivered_callback(refreshed.append)
    watched_at = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    try:
        with db.session() as session:
            for episode in (1, 2):
                service.enqueue_history(
                    session,
                    title_type="show",
                    trakt_id=50,
                    title="Example show",
                    desired_watched=True,
                    base_watched=False,
                    watched_at=watched_at,
                    season=1,
                    episode=episode,
                    episode_trakt_id=500 + episode,
                )

        result = service.drain()

        assert result.delivered == 2
        assert refreshed == [50]

        _enqueue_movie_watch(service, db, 51, watched_at)
        assert service.drain().delivered == 1
        assert refreshed == [50]
    finally:
        db.close()


def test_blocked_retry_discard_and_retry_after(tmp_path: Path) -> None:
    db = _database(tmp_path / "errors.sqlite3")
    client = _DeliveryClient()
    service, repository = _outbox_service(db, _Auth(client))
    try:
        with db.session() as session:
            service.enqueue_membership(
                session,
                operation_type="watchlist",
                title_type="movie",
                trakt_id=10,
                base_member=False,
                desired_member=True,
            )
        client.watchlist_error = TraktRequestError(422, "invalid object")
        service.drain()
        status = service.status()
        assert status["blocked"] == 1
        blocked_id = status["items"][0]["id"]

        service.retry()
        with db.session() as session:
            row = repository.get(session, "watchlist:movie:10")
            assert row is not None and row.status == OUTBOX_PENDING
        service.drain()
        assert service.discard_blocked(blocked_id) is True
        assert service.status()["total"] == 0

        with db.session() as session:
            service.enqueue_membership(
                session,
                operation_type="watchlist",
                title_type="movie",
                trakt_id=11,
                base_member=False,
                desired_member=True,
            )
        before = datetime.utcnow()
        client.watchlist_error = TraktRateLimitError("slow down", retry_after_seconds=37)
        service.drain()
        with db.session() as session:
            row = repository.get(session, "watchlist:movie:11")
            assert row is not None and row.status == OUTBOX_PENDING
            assert row.next_attempt_at is not None
            assert row.next_attempt_at >= before + timedelta(seconds=36)
    finally:
        db.close()


def test_all_local_mutations_work_without_authorization_and_progress_moves_backward(tmp_path: Path) -> None:
    db = _database(tmp_path / "local-first.sqlite3")
    auth = _Auth(authorized=False)
    outbox, repository = _outbox_service(db, auth)
    titles = TitleRepository()
    states = UserStateRepository()
    history = HistoryRepository()
    episodes = EpisodeRepository()
    progress = ProgressRepository()
    sync_state = SyncStateRepository()
    release_repository = ReleaseTrackingRepository()
    try:
        with db.session() as session:
            episodes.replace_show_episodes(
                session,
                42,
                [
                    EpisodeSummary(trakt_id=4201, season=1, number=1, title="One"),
                    EpisodeSummary(trakt_id=4202, season=1, number=2, title="Two"),
                ],
            )
        history_service = HistoryService(
            db,
            auth,
            titles,
            states,
            history,
            episodes,
            SimpleNamespace(),
            SimpleNamespace(),
            outbox,
            progress,
        )
        watched_at = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        history_service.add_history_items(
            [
                HistoryItemInput("show", 42, watched_at, season=1, episode=1, title="Offline Show"),
                HistoryItemInput("show", 42, watched_at, season=1, episode=2, title="Offline Show"),
            ]
        )
        history_service.set_rating(RatingInput("show", 42, 9), title="Offline Show")

        catalog = CatalogService(
            db,
            auth,
            titles,
            states,
            sync_state,
            lambda _config: SimpleNamespace(),
            SimpleNamespace(is_ready=lambda: False),
            history,
            outbox,
        )
        catalog.set_watchlisted(
            "show",
            42,
            watchlisted=True,
            snapshot={"title": "Offline Show", "released_at": "2026-07-01", "list_count": 23},
        )

        releases = ReleaseTrackingService(
            db,
            auth,
            SimpleNamespace(),
            release_repository,
            progress,
            SimpleNamespace(),
            titles=titles,
            trakt_outbox=outbox,
        )
        releases.set_tracked(
            "show",
            42,
            tracked=True,
            title="Offline Show",
            released_at="2026-08-01T00:00:00Z",
            list_count=12,
        )

        progress_workflow = ProgressSyncWorkflow(
            db,
            auth,
            progress,
            episodes,
            titles,
            states,
            sync_state,
            lambda _config: SimpleNamespace(),
            SimpleNamespace(),
            OperationLog(),
            SimpleNamespace(),
            history_repo=history,
            trakt_outbox=outbox,
        )
        progress_workflow.pause_show(42)
        progress_workflow.drop_show(42)

        with db.session() as session:
            row = session.scalar(select(WatchProgress).where(WatchProgress.show_trakt_id == 42))
            assert row is not None
            assert row.completed == 2
            assert row.next_episode_trakt_id is None
            state = states.progress_state(session, 42)
            assert state is not None and state.paused and state.archived
            operation_types = {item.operation_type for item in repository.list_items(session)}
            assert operation_types == {"history", "rating", "watchlist", "release", "paused", "dropped"}
        assert catalog.watchlist_keys() == {("show", 42)}
        assert catalog.local_watchlist_titles()[0].explore_metric_count == 23
        assert releases.local_keys() == {("show", 42)}

        auth.client = _DeliveryClient()
        auth.authorized = True
        assert [(item.title_type, item.trakt_id) for item in catalog.watchlist_titles()] == [("show", 42)]
        assert [(item.title_type, item.trakt_id) for item in releases.refresh()] == [("show", 42)]
        progress_workflow._sync_hidden_status()
        with db.session() as session:
            state = states.progress_state(session, 42)
            assert state is not None and state.paused and state.archived

        history_service.remove_episode_watch(show_trakt_id=42, season=1, episode=2)
        with db.session() as session:
            row = session.scalar(select(WatchProgress).where(WatchProgress.show_trakt_id == 42))
            assert row is not None
            assert row.completed == 1
            assert row.next_episode_trakt_id == 4202

        progress_workflow.resume_show(42)
        progress_workflow.undrop_show(42)
        catalog.set_watchlisted("show", 42, watchlisted=False)
        releases.set_tracked("show", 42, tracked=False)
        with db.session() as session:
            state = states.progress_state(session, 42)
            assert state is not None and not state.paused and not state.archived
            remaining_types = {item.operation_type for item in repository.list_items(session)}
            assert remaining_types == {"history", "rating"}
    finally:
        db.close()


def test_unknown_watch_date_is_local_only(tmp_path: Path) -> None:
    db = _database(tmp_path / "unknown-date.sqlite3")
    auth = _Auth(authorized=False)
    outbox, repository = _outbox_service(db, auth)
    history = HistoryRepository()
    try:
        service = HistoryService(
            db,
            auth,
            TitleRepository(),
            UserStateRepository(),
            history,
            EpisodeRepository(),
            SimpleNamespace(),
            SimpleNamespace(),
            outbox,
            ProgressRepository(),
        )
        service.add_history_item(HistoryItemInput("movie", 9, None, title="Undated"))
        with db.session() as session:
            assert session.scalar(select(func.count(HistoryEvent.id))) == 1
            assert repository.list_items(session) == []
    finally:
        db.close()


def test_trakt_stale_cache_precedes_token_but_authoritative_read_rejects_it(tmp_path: Path) -> None:
    provider = f"trakt_test_{tmp_path.name}"
    client = TraktClient(
        "client-id",
        "client-secret",
        "http://localhost",
        cache_provider=provider,
        cache_ttl_hours=-1,
    )
    key = client._make_cache_key("GET", "/cached", None, True)
    client._cache.set_json(key, {"cached": True})
    try:
        assert client._request("GET", "/cached") == {"cached": True}
        with pytest.raises(Exception, match="Authentication is required"):
            client._request("GET", "/cached", use_cache=False)
    finally:
        client.close()
        ProviderCache(provider).clear()


def test_authoritative_trakt_read_refreshes_last_good_without_touching_other_provider_cache(tmp_path: Path) -> None:
    trakt_provider = f"trakt_test_{tmp_path.name}"
    tmdb_provider = f"tmdb_test_{tmp_path.name}"
    tmdb_cache = ProviderCache(tmdb_provider)
    tmdb_cache.set_json("sentinel", {"tmdb": True})
    client = TraktClient(
        "client-id",
        "client-secret",
        "http://localhost",
        cache_provider=trakt_provider,
    )
    client._client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"fresh": True})))
    try:
        assert client._request("GET", "/public", auth_required=False, use_cache=False) == {"fresh": True}
        assert client.last_good_cache_at() is not None
        client.clear_cache()
        assert tmdb_cache.get_json("sentinel", 24) == {"tmdb": True}
    finally:
        client.close()
        ProviderCache(trakt_provider).clear()
        tmdb_cache.clear()
