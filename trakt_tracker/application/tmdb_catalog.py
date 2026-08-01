from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.application.trakt_outbox import TraktOutboxService
from trakt_tracker.domain import TitleSummary
from trakt_tracker.infrastructure.tmdb import (
    TMDB_BACKDROP_IMAGE_BASE,
    TMDB_POSTER_IMAGE_BASE,
    TMDbClient,
)
from trakt_tracker.infrastructure.notifications import NotificationMessage
from trakt_tracker.persistence.repositories import SyncStateRepository
from trakt_tracker.persistence.tmdb_preview import TmdbPreviewRepository


TMDB_PREVIEW_SEARCH_STATE_KEY = "tmdb_preview_search_state"
TMDB_PREVIEW_EXPLORE_STATE_KEY = "tmdb_preview_explore_state"


@dataclass(slots=True)
class TmdbCatalogItem:
    provider: str = "tmdb"
    title_type: str = "movie"
    tmdb_id: int = 0
    trakt_id: int | None = None
    imdb_id: str = ""
    title: str = ""
    year: int | None = None
    overview: str = ""
    status: str = ""
    slug: str = ""
    poster_url: str = ""
    backdrop_url: str = ""
    tmdb_rating: float | None = None
    tmdb_votes: int | None = None
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    ratings_status: str = ENRICH_STATUS_UNKNOWN
    popularity: float | None = None
    released_at: datetime | None = None
    explore_metric_kind: str = ""
    explore_metric_count: int | None = None
    explore_rank: int | None = None
    release_distance_text: str = ""
    is_watchlisted: bool = False
    is_in_history: bool = False
    is_release_tracked: bool = False
    release_acknowledged: bool = False
    is_future_release: bool = False
    is_notification_matured: bool = False
    local_only: bool = True
    catalog_actions_available: bool = True
    seasons: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tmdb_url(self) -> str:
        media = "tv" if self.title_type == "show" else "movie"
        return f"https://www.themoviedb.org/{media}/{int(self.tmdb_id)}"

    @property
    def source_url(self) -> str:
        if self.trakt_id:
            media = "shows" if self.title_type == "show" else "movies"
            return f"https://trakt.tv/{media}/{self.slug or int(self.trakt_id)}"
        return self.tmdb_url

    @property
    def identity_key(self) -> tuple[str, int]:
        return self.title_type, int(self.tmdb_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "title_type": self.title_type,
            "tmdb_id": int(self.tmdb_id),
            "trakt_id": int(self.trakt_id) if self.trakt_id else None,
            "imdb_id": self.imdb_id,
            "title": self.title,
            "year": self.year,
            "overview": self.overview,
            "status": self.status,
            "slug": self.slug,
            "poster_url": self.poster_url,
            "backdrop_url": self.backdrop_url,
            "tmdb_rating": self.tmdb_rating,
            "tmdb_votes": self.tmdb_votes,
            "imdb_rating": self.imdb_rating,
            "imdb_votes": self.imdb_votes,
            "ratings_status": self.ratings_status,
            "popularity": self.popularity,
            "released_at": self.released_at.isoformat() if self.released_at else "",
        }


@dataclass(slots=True)
class TmdbCatalogPage:
    items: list[TmdbCatalogItem]
    page: int
    page_count: int


class TmdbCatalogService:
    """Read-only TMDb catalog plus profile-local preview state.

    Existing Trakt services remain the authority whenever an item has a known
    Trakt identity.  Items without one never call Trakt; their state lives in
    the preview repository and can later be exported when a mapping appears.
    """

    def __init__(
        self,
        db,
        auth_service,
        tmdb_factory: Callable[[Any], TMDbClient],
        titles,
        preview_repository: TmdbPreviewRepository,
        trakt_outbox: TraktOutboxService | None = None,
        imdb_client=None,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._tmdb_factory = tmdb_factory
        self._titles = titles
        self._repository = preview_repository
        self._sync_state = SyncStateRepository()
        self._trakt_outbox = trakt_outbox
        self._imdb_client = imdb_client
        self._legacy_catalog = None
        self._legacy_release_tracking = None
        self._legacy_search_watch = None
        self._notification_sender = None

    def set_legacy_services(self, *, catalog=None, release_tracking=None, search_watch=None) -> None:
        self._legacy_catalog = catalog
        self._legacy_release_tracking = release_tracking
        self._legacy_search_watch = search_watch

    def is_configured(self) -> bool:
        return self._tmdb_factory(self._auth.config).is_configured()

    def search_titles(
        self,
        query: str,
        title_type: str | None = None,
        *,
        page: int = 1,
        limit: int = 24,
    ) -> TmdbCatalogPage:
        payload = self._tmdb_factory(self._auth.config).search_catalog(
            query,
            title_type=title_type,
            page=page,
        )
        result = self._page_from_payload(payload, title_type=title_type, page=page, limit=limit, metric_kind="")
        self._hydrate_imdb_metadata(result.items)
        return result

    def filtered_search_titles(
        self,
        query: str,
        title_type: str | None,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        tmdb_min: float | None,
        max_scan_pages: int,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
    ) -> TmdbCatalogPage:
        return self._filtered_catalog_page(
            lambda source_page: self.search_titles(
                query,
                title_type,
                page=source_page,
                limit=limit,
            ),
            page=page,
            limit=limit,
            max_scan_pages=max_scan_pages,
            imdb_min=imdb_min,
            tmdb_min=tmdb_min,
            hide_watchlisted=hide_watchlisted,
            hide_history=hide_history,
        )

    def load_search_state(self) -> dict[str, Any]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, TMDB_PREVIEW_SEARCH_STATE_KEY, "")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "query": str(payload.get("query") or ""),
            "title_type": str(payload.get("title_type") or "all"),
            "sort_mode": str(payload.get("sort_mode") or "IMDb votes"),
            "imdb_min": str(payload.get("imdb_min") or ""),
            "tmdb_min": str(payload.get("tmdb_min") or ""),
            "hide_watchlisted": bool(payload.get("hide_watchlisted", False)),
            "hide_history": bool(payload.get("hide_history", False)),
        }

    def save_search_state(
        self,
        *,
        query: str,
        title_type: str | None,
        sort_mode: str,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
        imdb_min: str = "",
        tmdb_min: str = "",
    ) -> None:
        payload = {
            "query": str(query or "").strip(),
            "title_type": title_type or "all",
            "sort_mode": str(sort_mode or "IMDb votes"),
            "imdb_min": str(imdb_min or ""),
            "tmdb_min": str(tmdb_min or ""),
            "hide_watchlisted": bool(hide_watchlisted),
            "hide_history": bool(hide_history),
        }
        with self._db.session() as session:
            self._sync_state.set_value(
                session,
                TMDB_PREVIEW_SEARCH_STATE_KEY,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )

    def load_explore_state(self) -> dict[str, Any]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, TMDB_PREVIEW_EXPLORE_STATE_KEY, "")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "imdb_min": str(payload.get("imdb_min") or ""),
            "tmdb_min": str(payload.get("tmdb_min") or ""),
            "hide_watchlisted": bool(payload.get("hide_watchlisted", False)),
            "hide_history": bool(payload.get("hide_history", False)),
            "hide_releases": bool(payload.get("hide_releases", False)),
        }

    def save_explore_state(
        self,
        *,
        imdb_min: str,
        tmdb_min: str,
        hide_watchlisted: bool,
        hide_history: bool,
        hide_releases: bool,
    ) -> None:
        payload = {
            "imdb_min": str(imdb_min or ""),
            "tmdb_min": str(tmdb_min or ""),
            "hide_watchlisted": bool(hide_watchlisted),
            "hide_history": bool(hide_history),
            "hide_releases": bool(hide_releases),
        }
        with self._db.session() as session:
            self._sync_state.set_value(
                session,
                TMDB_PREVIEW_EXPLORE_STATE_KEY,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )

    def explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int = 1,
        limit: int = 24,
    ) -> TmdbCatalogPage:
        client = self._tmdb_factory(self._auth.config)
        if feed == "trending":
            payload = client.trending_catalog(title_type, page=page)
            metric_kind = "weekly trend"
        elif feed == "anticipated":
            payload = client.discover_catalog(title_type, page=page, upcoming=True)
            metric_kind = "popularity"
        else:
            payload = client.discover_catalog(title_type, page=page, upcoming=False)
            metric_kind = "popularity"
        result = self._page_from_payload(
            payload,
            title_type=title_type,
            page=page,
            limit=limit,
            metric_kind=metric_kind,
        )
        self._hydrate_imdb_metadata(result.items)
        return result

    def filtered_explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        tmdb_min: float | None,
        max_scan_pages: int,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
        hide_releases: bool = False,
    ) -> TmdbCatalogPage:
        return self._filtered_catalog_page(
            lambda source_page: self.explore_titles(
                title_type,
                feed,
                page=source_page,
                limit=limit,
            ),
            page=page,
            limit=limit,
            max_scan_pages=max_scan_pages,
            imdb_min=imdb_min,
            tmdb_min=tmdb_min,
            hide_watchlisted=hide_watchlisted,
            hide_history=hide_history,
            hide_releases=hide_releases,
        )

    def local_release_items(self) -> list[TmdbCatalogItem]:
        result: dict[tuple[str, int], TmdbCatalogItem] = {}
        with self._db.session() as session:
            for row in self._repository.list_release_states(session):
                snapshot = self._repository.snapshot(session, row.title_type, row.tmdb_id)
                item = self._item_from_snapshot(snapshot, row=row)
                if item is not None:
                    result[item.identity_key] = item
            if self._legacy_release_tracking is not None:
                try:
                    legacy_items = self._legacy_release_tracking.local_items()
                except Exception:
                    legacy_items = []
                for legacy in legacy_items:
                    tmdb_id = int(getattr(legacy, "tmdb_id", 0) or 0)
                    if tmdb_id <= 0:
                        continue
                    item = self._item_from_legacy(legacy)
                    result[item.identity_key] = item
                    self._repository.upsert_snapshot(session, item.snapshot())
                    release_row = self._repository.upsert_release_state(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        title=item.title,
                        release_at=item.released_at,
                        list_count=getattr(legacy, "explore_metric_count", None),
                    )
                    release_row.acknowledged_at = (
                        datetime.now(tz=UTC).replace(tzinfo=None)
                        if bool(getattr(legacy, "release_acknowledged", False))
                        else None
                    )
        self._decorate_local_state(result.values())
        items = list(result.values())
        self._hydrate_imdb_metadata(items, resolve_missing_ids=False)
        return items

    def refresh_release_items(self) -> list[TmdbCatalogItem]:
        items = self.local_release_items()
        client = self._tmdb_factory(self._auth.config)
        refreshed: list[TmdbCatalogItem] = []
        for item in items:
            try:
                payload = client.get_catalog_details(item.title_type, item.tmdb_id)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                updated = self._item_from_payload(payload, title_type=item.title_type)
                if updated is not None:
                    updated.trakt_id = item.trakt_id
                    updated.is_release_tracked = True
                    item = updated
                    with self._db.session() as session:
                        self._repository.upsert_snapshot(session, item.snapshot())
                        self._repository.upsert_release_state(
                            session,
                            title_type=item.title_type,
                            tmdb_id=item.tmdb_id,
                            title=item.title,
                            release_at=item.released_at,
                            list_count=item.explore_metric_count,
                        )
            refreshed.append(item)
        self._decorate_local_state(refreshed)
        return refreshed

    def watchlist_state(self, title_type: str, tmdb_id: int) -> bool:
        with self._db.session() as session:
            return self._repository.effective_state(
                session,
                operation_type="watchlist",
                title_type=title_type,
                tmdb_id=tmdb_id,
            )

    def history_state(self, title_type: str, tmdb_id: int, season: int | None = None, episode: int | None = None) -> bool:
        with self._db.session() as session:
            return self._repository.effective_state(
                session,
                operation_type="history",
                title_type=title_type,
                tmdb_id=tmdb_id,
                season=season,
                episode=episode,
            )

    def release_state(self, title_type: str, tmdb_id: int) -> bool:
        with self._db.session() as session:
            return self._repository.effective_state(
                session,
                operation_type="release",
                title_type=title_type,
                tmdb_id=tmdb_id,
            )

    def set_release_acknowledged(
        self,
        title_type: str,
        tmdb_id: int,
        *,
        acknowledged: bool,
        title: str = "",
        release_at: datetime | None = None,
    ) -> bool:
        with self._db.session() as session:
            if self._repository.release_state(session, str(title_type), int(tmdb_id)) is None and title:
                self._repository.upsert_release_state(
                    session,
                    title_type=str(title_type),
                    tmdb_id=int(tmdb_id),
                    title=title,
                    release_at=release_at,
                )
            return self._repository.set_release_acknowledged(
                session,
                str(title_type),
                int(tmdb_id),
                bool(acknowledged),
            )

    def poll_releases(self, *, send_native: bool = True, refresh_remote: bool = True) -> list[dict[str, Any]]:
        """Poll preview release state without touching Trakt.

        The timing policy intentionally mirrors ``ReleaseTrackingService``;
        only the durable state namespace differs.
        """
        if refresh_remote:
            self.refresh_release_items()
        config = self._auth.config
        if not bool(getattr(config, "notifications_enabled", True)):
            return []
        now = datetime.now(tz=UTC)
        repeat = timedelta(minutes=max(1, int(getattr(config, "notification_repeat_minutes", 5) or 5)))
        sent: list[dict[str, Any]] = []
        with self._db.session() as session:
            for row in self._repository.list_release_states(session):
                release_at = _as_utc(row.release_at)
                if release_at is None or release_at > now or row.acknowledged_at is not None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now < release_at + timedelta(minutes=max(0, delay_minutes)):
                    continue
                last_sent = _as_utc(row.last_sent_at)
                if last_sent is not None and now - last_sent < repeat:
                    continue
                body = "Movie is now available" if row.title_type == "movie" else "Show has premiered"
                if send_native:
                    # NotificationMessage is the same native-notification path
                    # used by the Trakt release tracker.
                    self._send_notification(row.title, body)
                self._repository.mark_release_sent(session, row.title_type, int(row.tmdb_id))
                sent.append({"show_title": row.title, "message": body, "source": "release"})
        return sent

    def released_count(self) -> int:
        now = datetime.now(tz=UTC)
        with self._db.session() as session:
            return sum(
                1
                for row in self._repository.list_release_states(session)
                if (release_at := _as_utc(row.release_at)) is not None and release_at <= now
            )

    def has_due_unacknowledged_release(self) -> bool:
        now = datetime.now(tz=UTC)
        config = self._auth.config
        with self._db.session() as session:
            for row in self._repository.list_release_states(session):
                release_at = _as_utc(row.release_at)
                if release_at is None or row.acknowledged_at is not None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now >= release_at + timedelta(minutes=max(0, delay_minutes)):
                    return True
        return False

    def matured_release_keys(self) -> set[tuple[str, int]]:
        now = datetime.now(tz=UTC)
        config = self._auth.config
        with self._db.session() as session:
            result: set[tuple[str, int]] = set()
            for row in self._repository.list_release_states(session):
                release_at = _as_utc(row.release_at)
                if release_at is None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now >= release_at + timedelta(minutes=max(0, delay_minutes)):
                    result.add((str(row.title_type), int(row.tmdb_id)))
            return result

    def get_item(self, title_type: str, tmdb_id: int) -> TmdbCatalogItem:
        mapping_changed = False
        with self._db.session() as session:
            snapshot = self._repository.snapshot(session, title_type, tmdb_id)
            if snapshot is not None:
                item = self._item_from_snapshot(snapshot)
                identity = self._repository.identity(session, title_type, tmdb_id)
                stored = self._titles.by_tmdb_id(session, title_type, tmdb_id)
                if stored is not None and (identity is None or not identity.trakt_id):
                    if item is not None:
                        item.slug = str(stored.slug or "")
                    identity = self._repository.set_identity(
                        session,
                        title_type=title_type,
                        tmdb_id=tmdb_id,
                        trakt_id=int(stored.trakt_id),
                        imdb_id=str(stored.imdb_id or getattr(identity, "imdb_id", "") or ""),
                    )
                if item is not None and identity is not None:
                    item.trakt_id = int(identity.trakt_id) if identity.trakt_id else None
                    item.imdb_id = str(identity.imdb_id or item.imdb_id or "")
                if item is not None and identity is not None and identity.trakt_id:
                    if not item.slug:
                        stored = self._titles.get_title(session, int(identity.trakt_id))
                        item.slug = str(stored.slug or "") if stored is not None else ""
                    mapping_changed = bool(
                        self._repository.attach_mapping(
                            session,
                            title_type=title_type,
                            tmdb_id=tmdb_id,
                            trakt_id=int(identity.trakt_id),
                        )
                    )
            else:
                item = None
        if snapshot is None:
            try:
                payload = self._tmdb_factory(self._auth.config).get_catalog_details(title_type, tmdb_id) or {}
            except Exception:
                payload = {}
            item = self._item_from_payload(payload, title_type=title_type) if payload else None
        if item is None:
            item = TmdbCatalogItem(title_type=title_type, tmdb_id=int(tmdb_id), title=f"TMDb {tmdb_id}")
        item.local_only = item.trakt_id is None
        if mapping_changed:
            self.reconcile_mapped_intents()
        self._hydrate_imdb_metadata([item], resolve_missing_ids=True)
        self._decorate_local_state([item])
        return item

    def set_watchlisted(self, item: TmdbCatalogItem, watchlisted: bool) -> dict[str, Any]:
        mapped = int(item.trakt_id or 0) or None
        if mapped and self._legacy_catalog is not None:
            self._legacy_catalog.set_watchlisted(
                item.title_type,
                mapped,
                watchlisted=watchlisted,
                snapshot={
                    "title": item.title,
                    "released_at": item.released_at.isoformat() if item.released_at else "",
                    "list_count": item.explore_metric_count,
                },
            )
            return {"watchlisted": bool(watchlisted), "local_only": False, "trakt_id": mapped}
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            row = self._repository.set_intent(
                session,
                operation_type="watchlist",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                desired=watchlisted,
                payload=item.snapshot(),
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"watchlisted": bool(watchlisted), "local_only": mapped is None, "trakt_id": mapped}

    def set_release_tracked(self, item: TmdbCatalogItem, tracked: bool) -> dict[str, Any]:
        mapped = int(item.trakt_id or 0) or None
        if mapped and self._legacy_release_tracking is not None:
            self._legacy_release_tracking.set_tracked(
                item.title_type,
                mapped,
                tracked=tracked,
                list_count=item.explore_metric_count,
                title=item.title,
                released_at=item.released_at,
            )
            with self._db.session() as session:
                if tracked:
                    self._repository.upsert_release_state(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        title=item.title,
                        release_at=item.released_at,
                        list_count=item.explore_metric_count,
                    )
                else:
                    state = self._repository.release_state(session, item.title_type, item.tmdb_id)
                    if state is not None:
                        session.delete(state)
            return {"tracked": bool(tracked), "local_only": False, "trakt_id": mapped}
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            if tracked:
                self._repository.upsert_release_state(
                    session,
                    title_type=item.title_type,
                    tmdb_id=item.tmdb_id,
                    title=item.title,
                    release_at=item.released_at,
                    list_count=item.explore_metric_count,
                )
            else:
                state = self._repository.release_state(session, item.title_type, item.tmdb_id)
                if state is not None:
                    session.delete(state)
            row = self._repository.set_intent(
                session,
                operation_type="release",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                desired=tracked,
                payload=item.snapshot(),
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"tracked": bool(tracked), "local_only": mapped is None, "trakt_id": mapped}

    def mark_watched(
        self,
        item: TmdbCatalogItem,
        *,
        watched_at: datetime | None,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        mapped = int(item.trakt_id or 0) or None
        if mapped and self._legacy_catalog is not None:
            return {"local_only": False, "trakt_id": mapped, "mapped": True}
        if watched_at is None:
            watched_at = datetime.now(tz=UTC)
        payload = item.snapshot()
        payload.update({"watched_at": watched_at.astimezone(UTC).isoformat(), "season": season, "episode": episode})
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            row = self._repository.set_intent(
                session,
                operation_type="history",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                season=season,
                episode=episode,
                desired=True,
                payload=payload,
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"local_only": mapped is None, "trakt_id": mapped, "mapped": False}

    def unwatch(
        self,
        item: TmdbCatalogItem,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        mapped = int(item.trakt_id or 0) or None
        if mapped and self._legacy_catalog is not None:
            return {"local_only": False, "trakt_id": mapped, "mapped": True}
        with self._db.session() as session:
            row = self._repository.set_intent(
                session,
                operation_type="history",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                season=season,
                episode=episode,
                desired=False,
                payload=item.snapshot(),
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"local_only": mapped is None, "trakt_id": mapped, "mapped": False}

    def load_watch_panel(self, tmdb_id: int, *, season: int | None = None) -> dict[str, Any]:
        mapped_item = self.get_item("show", tmdb_id)
        if mapped_item.trakt_id and self._legacy_search_watch is not None:
            panel = self._legacy_search_watch.load_show_panel(
                int(mapped_item.trakt_id),
                default_season=season,
            )
            selected_season = season
            if selected_season is None:
                selected_season = next(
                    (
                        int(value.season)
                        for value in panel.seasons
                        if bool(getattr(value, "is_default", False))
                    ),
                    None,
                )
            if selected_season is None and panel.seasons:
                selected_season = int(panel.seasons[0].season)
            selected = next(
                (value for value in panel.seasons if int(value.season) == int(selected_season or 0)),
                None,
            )
            episodes = []
            if selected is not None:
                episodes = [
                    {
                        "season": int(value.season),
                        "episode": int(value.number),
                        "title": str(value.title or ""),
                        "overview": "",
                        "first_aired": value.first_aired,
                        "still_url": str(value.still_url or ""),
                        "watched": bool(value.is_watched),
                    }
                    for value in selected.episodes
                ]
            return {
                "tmdb_id": int(tmdb_id),
                "trakt_id": int(mapped_item.trakt_id),
                "title": str(panel.title or mapped_item.title),
                "overview": mapped_item.overview,
                "seasons": [
                    {
                        "season_number": int(value.season),
                        "episode_count": len(value.episodes),
                    }
                    for value in panel.seasons
                ],
                "selected_season": selected_season,
                "episodes": episodes,
            }
        client = self._tmdb_factory(self._auth.config)
        details = client.get_catalog_details("show", tmdb_id) or {}
        seasons = [
            item
            for item in details.get("seasons", [])
            if isinstance(item, dict) and int(item.get("season_number") or -1) >= 0
        ]
        selected = season
        if selected is None:
            selected = next((int(item.get("season_number")) for item in seasons if int(item.get("episode_count") or 0) > 0), 1)
        payload = client.get_catalog_season(tmdb_id, selected) or {}
        episodes: list[dict[str, Any]] = []
        with self._db.session() as session:
            for raw in payload.get("episodes", []) if isinstance(payload, dict) else []:
                if not isinstance(raw, dict):
                    continue
                number = int(raw.get("episode_number") or 0)
                if number <= 0:
                    continue
                episodes.append(
                    {
                        "season": selected,
                        "episode": number,
                        "title": str(raw.get("name") or ""),
                        "overview": str(raw.get("overview") or ""),
                        "first_aired": _parse_datetime(raw.get("air_date")),
                        "still_url": self._still_url(raw.get("still_path")),
                        "watched": self._repository.effective_state(
                            session,
                            operation_type="history",
                            title_type="show",
                            tmdb_id=tmdb_id,
                            season=selected,
                            episode=number,
                        ),
                    }
                )
        return {
            "tmdb_id": int(tmdb_id),
            "title": str(details.get("name") or ""),
            "overview": str(details.get("overview") or ""),
            "seasons": seasons,
            "selected_season": selected,
            "episodes": episodes,
        }

    def reconcile_mapped_intents(self) -> int:
        """Move local preview intents with a known Trakt id into the normal outbox."""
        if self._trakt_outbox is None:
            return 0
        moved = 0
        with self._db.session() as session:
            rows = self._repository.list_intents(session)
            for row in rows:
                if row.mapped_trakt_id is None or row.status == "exported":
                    continue
                payload = _json_dict(row.payload_json)
                desired = bool(_json_value(row.desired_state_json, False))
                operation = str(row.operation_type)
                key: str | None = None
                if operation in {"watchlist", "release"}:
                    base_member = self._mapped_membership_base(
                        operation,
                        row.title_type,
                        int(row.mapped_trakt_id),
                    )
                    key = self._trakt_outbox.enqueue_membership(
                        session,
                        operation_type="watchlist" if operation == "watchlist" else "release",
                        title_type=row.title_type,
                        trakt_id=int(row.mapped_trakt_id),
                        base_member=base_member,
                        desired_member=desired,
                        snapshot=payload,
                        origin="tmdb_preview",
                        metadata={
                            "tmdb_preview_intent_id": int(row.id),
                            "tmdb_preview_revision": int(row.revision or 0),
                        },
                    )
                elif operation == "history":
                    watched_at = _parse_datetime(payload.get("watched_at"))
                    base_watched = self._mapped_history_base(row.title_type, int(row.mapped_trakt_id))
                    if desired and watched_at is None:
                        watched_at = datetime.now(tz=UTC)
                    if desired or base_watched:
                        key = self._trakt_outbox.enqueue_history(
                            session,
                            title_type=row.title_type,
                            trakt_id=int(row.mapped_trakt_id),
                            title=str(payload.get("title") or ""),
                            desired_watched=True,
                            base_watched=base_watched,
                            watched_at=watched_at,
                            season=_optional_int(payload.get("season")),
                            episode=_optional_int(payload.get("episode")),
                            origin="tmdb_preview",
                            metadata={
                                "tmdb_preview_intent_id": int(row.id),
                                "tmdb_preview_revision": int(row.revision or 0),
                            },
                        )
                if key:
                    row.status = "exported"
                    moved += 1
                elif row.status == "mapped_pending":
                    # The desired state already equals the known Trakt base;
                    # there is no POST to await, so this revision is complete.
                    row.status = "exported"
        if moved:
            self._trakt_outbox.wake()
        return moved

    def _mapped_membership_base(self, operation: str, title_type: str, trakt_id: int) -> bool:
        try:
            if operation == "watchlist" and self._legacy_catalog is not None:
                return (str(title_type), int(trakt_id)) in self._legacy_catalog.watchlist_keys()
            if operation == "release" and self._legacy_release_tracking is not None:
                return (str(title_type), int(trakt_id)) in self._legacy_release_tracking.local_keys()
        except Exception:
            pass
        return False

    def _mapped_history_base(self, title_type: str, trakt_id: int) -> bool:
        try:
            if self._legacy_catalog is not None:
                return (str(title_type), int(trakt_id)) in self._legacy_catalog.history_keys()
        except Exception:
            pass
        return False

    def on_trakt_outbox_delivered(self, operation) -> None:
        """Drop only the preview revision whose Trakt delivery was confirmed."""
        payload = getattr(operation, "payload", {})
        if not isinstance(payload, dict) or str(getattr(operation, "origin", "")) != "tmdb_preview":
            return
        intent_id = _optional_int(payload.get("tmdb_preview_intent_id"))
        revision = _optional_int(payload.get("tmdb_preview_revision"))
        if intent_id is None or revision is None:
            return
        with self._db.session() as session:
            self._repository.delete_intent_if_revision(session, intent_id, revision)

    def clear(self) -> None:
        with self._db.session() as session:
            self._repository.clear(session)
            self._sync_state.set_value(session, TMDB_PREVIEW_SEARCH_STATE_KEY, "")
            self._sync_state.set_value(session, TMDB_PREVIEW_EXPLORE_STATE_KEY, "")
        client = self._tmdb_factory(self._auth.config)
        clear_cache = getattr(client, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()

    def _send_notification(self, title: str, body: str) -> None:
        # Importing the concrete sender lazily keeps the catalog service easy to
        # exercise with a lightweight auth/client double in unit tests.
        sender = getattr(self, "_notification_sender", None)
        if sender is not None:
            sender.send(NotificationMessage(title=title, body=body))

    def set_notification_sender(self, sender) -> None:
        self._notification_sender = sender

    def _filtered_catalog_page(
        self,
        fetch_page: Callable[[int], TmdbCatalogPage],
        *,
        page: int,
        limit: int,
        max_scan_pages: int,
        imdb_min: float | None,
        tmdb_min: float | None,
        hide_watchlisted: bool,
        hide_history: bool,
        hide_releases: bool = False,
    ) -> TmdbCatalogPage:
        current_page = max(1, int(page))
        page_size = max(1, int(limit))
        target_count = current_page * page_size + 1
        matches: list[TmdbCatalogItem] = []
        source_page_number = 1
        exhausted = False
        while (
            len(matches) < target_count
            and not exhausted
            and source_page_number <= max(1, int(max_scan_pages))
        ):
            source_page = fetch_page(source_page_number)
            matches.extend(
                item
                for item in source_page.items
                if (
                    (imdb_min is None or (item.imdb_rating is not None and item.imdb_rating >= imdb_min))
                    and (tmdb_min is None or (item.tmdb_rating is not None and item.tmdb_rating >= tmdb_min))
                    and (not hide_watchlisted or not item.is_watchlisted)
                    and (not hide_history or not item.is_in_history)
                    and (not hide_releases or not item.is_release_tracked)
                )
            )
            exhausted = source_page_number >= source_page.page_count
            source_page_number += 1
        start = (current_page - 1) * page_size
        end = start + page_size
        has_next = len(matches) > end
        return TmdbCatalogPage(
            items=matches[start:end],
            page=current_page,
            page_count=current_page + 1 if has_next else current_page,
        )

    def _page_from_payload(
        self,
        payload: dict[str, Any],
        *,
        title_type: str | None,
        page: int,
        limit: int,
        metric_kind: str,
    ) -> TmdbCatalogPage:
        raw_items = payload.get("results", []) if isinstance(payload, dict) else []
        items: list[TmdbCatalogItem] = []
        payload_page = _optional_int(payload.get("page")) or max(1, int(page))
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            item_type = self._item_type(raw, title_type)
            if item_type is None:
                continue
            item = self._item_from_payload(
                raw,
                title_type=item_type,
                metric_kind=metric_kind,
                explore_rank=((payload_page - 1) * 20 + index + 1) if metric_kind == "weekly trend" else None,
            )
            if item is not None:
                items.append(item)
        total_pages = _optional_int(payload.get("total_pages")) or max(1, int(page))
        return TmdbCatalogPage(items=items[: max(1, int(limit))], page=max(1, int(page)), page_count=max(1, total_pages))

    def _item_from_payload(
        self,
        payload: dict[str, Any],
        *,
        title_type: str,
        metric_kind: str = "",
        explore_rank: int | None = None,
    ) -> TmdbCatalogItem | None:
        tmdb_id = _optional_int(payload.get("id"))
        if tmdb_id is None or tmdb_id <= 0:
            return None
        released_at = _parse_datetime(payload.get("release_date") or payload.get("first_air_date"))
        title = str(payload.get("title") or payload.get("name") or "").strip()
        external_ids = payload.get("external_ids", {})
        item = TmdbCatalogItem(
            title_type="show" if title_type == "show" else "movie",
            tmdb_id=tmdb_id,
            title=title,
            year=_year(released_at),
            overview=str(payload.get("overview") or ""),
            status=str(payload.get("status") or ""),
            poster_url=self._poster_url(payload.get("poster_path")),
            backdrop_url=self._backdrop_url(payload.get("backdrop_path")),
            tmdb_rating=_optional_float(payload.get("vote_average")),
            tmdb_votes=_optional_int(payload.get("vote_count")),
            imdb_id=str(external_ids.get("imdb_id") or "") if isinstance(external_ids, dict) else "",
            popularity=_optional_float(payload.get("popularity")),
            released_at=released_at,
            explore_metric_kind=metric_kind,
            explore_rank=explore_rank,
            is_future_release=bool(released_at and released_at > datetime.now(tz=UTC)),
            is_notification_matured=self._release_is_matured(released_at, title_type),
        )
        mapping_changed = False
        with self._db.session() as session:
            identity = self._repository.identity(session, item.title_type, item.tmdb_id)
            stored = self._titles.by_tmdb_id(session, item.title_type, item.tmdb_id)
            if stored is not None and (identity is None or not identity.trakt_id):
                item.slug = str(stored.slug or "")
                identity = self._repository.set_identity(
                    session,
                    title_type=item.title_type,
                    tmdb_id=item.tmdb_id,
                    trakt_id=int(stored.trakt_id),
                    imdb_id=str(stored.imdb_id or item.imdb_id or getattr(identity, "imdb_id", "") or ""),
                )
            elif item.imdb_id and (identity is None or not identity.imdb_id):
                identity = self._repository.set_identity(
                    session,
                    title_type=item.title_type,
                    tmdb_id=item.tmdb_id,
                    trakt_id=int(identity.trakt_id) if identity is not None and identity.trakt_id else None,
                    imdb_id=item.imdb_id,
                )
            if identity is not None:
                item.trakt_id = int(identity.trakt_id) if identity.trakt_id else None
                item.imdb_id = str(identity.imdb_id or item.imdb_id or "")
                if item.trakt_id:
                    if not item.slug:
                        stored = self._titles.get_title(session, int(item.trakt_id))
                        item.slug = str(stored.slug or "") if stored is not None else ""
                    if stored is not None:
                        item.imdb_rating = stored.imdb_rating
                        item.imdb_votes = stored.imdb_votes
                        item.ratings_status = str(stored.ratings_status or ENRICH_STATUS_UNKNOWN)
                    mapping_changed = bool(self._repository.attach_mapping(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        trakt_id=item.trakt_id,
                    ))
            self._repository.upsert_snapshot(session, item.snapshot())
            item.is_watchlisted = self._repository.effective_state(
                session, operation_type="watchlist", title_type=item.title_type, tmdb_id=item.tmdb_id
            )
            item.is_in_history = self._repository.effective_state(
                session, operation_type="history", title_type=item.title_type, tmdb_id=item.tmdb_id
            )
            item.is_release_tracked = self._repository.effective_state(
                session, operation_type="release", title_type=item.title_type, tmdb_id=item.tmdb_id
            ) or self._repository.release_state(session, item.title_type, item.tmdb_id) is not None
            release_row = self._repository.release_state(session, item.title_type, item.tmdb_id)
            item.release_acknowledged = bool(release_row and release_row.acknowledged_at)
        item.local_only = item.trakt_id is None
        if mapping_changed:
            self.reconcile_mapped_intents()
        return item

    def _item_from_snapshot(self, snapshot, *, row=None) -> TmdbCatalogItem | None:
        if snapshot is None:
            return None
        stored_payload = _json_dict(snapshot.payload_json)
        item = TmdbCatalogItem(
            title_type=str(snapshot.title_type),
            tmdb_id=int(snapshot.tmdb_id),
            trakt_id=int(snapshot.trakt_id) if snapshot.trakt_id else None,
            imdb_id=str(snapshot.imdb_id or ""),
            title=str(snapshot.title or ""),
            slug=str(stored_payload.get("slug") or ""),
            year=snapshot.year,
            overview=str(snapshot.overview or ""),
            status=str(snapshot.status or ""),
            poster_url=str(snapshot.poster_url or ""),
            backdrop_url=str(snapshot.backdrop_url or ""),
            tmdb_rating=snapshot.tmdb_rating,
            tmdb_votes=snapshot.tmdb_votes,
            imdb_rating=_optional_float(stored_payload.get("imdb_rating")),
            imdb_votes=_optional_int(stored_payload.get("imdb_votes")),
            ratings_status=str(stored_payload.get("ratings_status") or ENRICH_STATUS_UNKNOWN),
            popularity=snapshot.popularity,
            released_at=_as_utc(snapshot.released_at),
            is_future_release=bool(snapshot.released_at and _as_utc(snapshot.released_at) > datetime.now(tz=UTC)),
            is_notification_matured=self._release_is_matured(snapshot.released_at, snapshot.title_type),
            is_release_tracked=row is not None,
            release_acknowledged=bool(row and row.acknowledged_at),
        )
        item.local_only = item.trakt_id is None
        return item

    def _item_from_legacy(self, legacy) -> TmdbCatalogItem:
        released_at = _as_utc(getattr(legacy, "released_at", None))
        return TmdbCatalogItem(
            title_type=str(legacy.title_type),
            tmdb_id=int(legacy.tmdb_id),
            trakt_id=int(legacy.trakt_id),
            imdb_id=str(getattr(legacy, "imdb_id", "") or ""),
            title=str(legacy.title or ""),
            slug=str(getattr(legacy, "slug", "") or ""),
            year=getattr(legacy, "year", None),
            overview=str(getattr(legacy, "overview", "") or ""),
            poster_url=str(getattr(legacy, "poster_url", "") or ""),
            backdrop_url=str(getattr(legacy, "backdrop_url", "") or ""),
            tmdb_rating=getattr(legacy, "tmdb_rating", None),
            tmdb_votes=getattr(legacy, "tmdb_votes", None),
            imdb_rating=getattr(legacy, "imdb_rating", None),
            imdb_votes=getattr(legacy, "imdb_votes", None),
            ratings_status=str(getattr(legacy, "ratings_status", ENRICH_STATUS_UNKNOWN) or ENRICH_STATUS_UNKNOWN),
            explore_metric_kind="lists",
            explore_metric_count=getattr(legacy, "explore_metric_count", None),
            released_at=released_at,
            is_release_tracked=True,
            release_acknowledged=bool(getattr(legacy, "release_acknowledged", False)),
            is_future_release=bool(released_at and released_at > datetime.now(tz=UTC)),
            is_notification_matured=self._release_is_matured(released_at, legacy.title_type),
            local_only=False,
        )

    def _hydrate_imdb_metadata(
        self,
        items: list[TmdbCatalogItem],
        *,
        resolve_missing_ids: bool = True,
    ) -> None:
        if not items:
            return
        unresolved: list[TmdbCatalogItem] = []
        with self._db.session() as session:
            for item in items:
                identity = self._repository.identity(session, item.title_type, item.tmdb_id)
                if identity is not None and identity.imdb_id:
                    item.imdb_id = str(identity.imdb_id)
                elif identity is not None and str(identity.status or "") == ENRICH_STATUS_CHECKED_NO_DATA:
                    item.ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
                elif (
                    resolve_missing_ids
                    and not item.imdb_id
                    and (identity is None or str(identity.status or "") != ENRICH_STATUS_CHECKED_NO_DATA)
                ):
                    unresolved.append(item)

        if unresolved:
            client = self._tmdb_factory(self._auth.config)

            def load_details(item: TmdbCatalogItem) -> tuple[TmdbCatalogItem, dict[str, Any] | None]:
                try:
                    payload = client.get_catalog_details(item.title_type, item.tmdb_id)
                except Exception:
                    payload = None
                return item, payload if isinstance(payload, dict) else None

            workers = min(6, len(unresolved))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tmdb-imdb-id") as pool:
                resolved = list(pool.map(load_details, unresolved))
            with self._db.session() as session:
                for item, payload in resolved:
                    external_ids = payload.get("external_ids", {}) if payload else {}
                    imdb_id = str(external_ids.get("imdb_id") or "") if isinstance(external_ids, dict) else ""
                    if imdb_id:
                        item.imdb_id = imdb_id
                    elif payload is not None:
                        item.ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
                    identity = self._repository.identity(session, item.title_type, item.tmdb_id)
                    self._repository.set_identity(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        trakt_id=(
                            int(identity.trakt_id)
                            if identity is not None and identity.trakt_id
                            else (int(item.trakt_id) if item.trakt_id else None)
                        ),
                        imdb_id=item.imdb_id,
                        status=(
                            ENRICH_STATUS_READY
                            if item.imdb_id
                            else (ENRICH_STATUS_CHECKED_NO_DATA if payload is not None else ENRICH_STATUS_UNKNOWN)
                        ),
                    )

        imdb_ready = bool(self._imdb_client is not None and self._imdb_client.is_ready())
        for item in items:
            if item.imdb_rating is not None and item.imdb_votes is not None:
                item.ratings_status = ENRICH_STATUS_READY
                continue
            if imdb_ready and item.imdb_id:
                summary = TitleSummary(
                    trakt_id=int(item.trakt_id or 0),
                    title_type=item.title_type,
                    title=item.title,
                    imdb_id=item.imdb_id,
                )
                summary = self._imdb_client.enrich_title(summary)
                item.imdb_rating = summary.imdb_rating
                item.imdb_votes = summary.imdb_votes
                item.ratings_status = (
                    ENRICH_STATUS_READY
                    if item.imdb_rating is not None and item.imdb_votes is not None
                    else ENRICH_STATUS_CHECKED_NO_DATA
                )
        with self._db.session() as session:
            for item in items:
                self._repository.upsert_snapshot(session, item.snapshot())

    def _decorate_local_state(self, items) -> None:
        with self._db.session() as session:
            for item in items:
                item.is_watchlisted = item.is_watchlisted or self._repository.effective_state(
                    session, operation_type="watchlist", title_type=item.title_type, tmdb_id=item.tmdb_id
                )
                item.is_in_history = item.is_in_history or self._repository.effective_state(
                    session, operation_type="history", title_type=item.title_type, tmdb_id=item.tmdb_id
                )
                item.is_release_tracked = item.is_release_tracked or self._repository.effective_state(
                    session, operation_type="release", title_type=item.title_type, tmdb_id=item.tmdb_id
                )

    def _wake_if_mapped(self, row) -> None:
        if row is not None and row.mapped_trakt_id is not None and self._trakt_outbox is not None:
            self.reconcile_mapped_intents()

    def _release_is_matured(self, release_at: datetime | None, title_type: str) -> bool:
        release_at = _as_utc(release_at)
        if release_at is None:
            return False
        config = self._auth.config
        delay_minutes = (
            int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
            if title_type == "movie"
            else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
        )
        return datetime.now(tz=UTC) >= release_at + timedelta(minutes=max(0, delay_minutes))

    @staticmethod
    def _item_type(payload: dict[str, Any], requested: str | None) -> str | None:
        if requested in {"movie", "show"}:
            return requested
        media_type = str(payload.get("media_type") or "")
        if media_type == "movie" or "title" in payload:
            return "movie"
        if media_type == "tv" or "name" in payload:
            return "show"
        return None

    @staticmethod
    def _poster_url(path: Any) -> str:
        return f"{TMDB_POSTER_IMAGE_BASE}{path}" if isinstance(path, str) and path else ""

    @staticmethod
    def _backdrop_url(path: Any) -> str:
        return f"{TMDB_BACKDROP_IMAGE_BASE}{path}" if isinstance(path, str) and path else ""

    @staticmethod
    def _still_url(path: Any) -> str:
        return f"https://image.tmdb.org/t/p/w780{path}" if isinstance(path, str) and path else ""


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _year(value: datetime | None) -> int | None:
    return value.year if value is not None else None


def _json_value(raw: str, fallback: Any) -> Any:
    import json

    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _json_dict(raw: str) -> dict[str, Any]:
    value = _json_value(raw, {})
    return value if isinstance(value, dict) else {}
