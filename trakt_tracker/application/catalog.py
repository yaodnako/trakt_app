from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime
from threading import Lock
from typing import Callable

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.application.enrich_queue import TASK_RESULT_SKIPPED_ALREADY_RESOLVED
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_BACKDROP,
    ASSET_KIND_POSTER,
    ASSET_KIND_TITLE_RATINGS,
    TITLE_ALLOWED_PARTS,
    TRIGGER_MANUAL_REPAIR,
    TRIGGER_SYNC_EVENT,
    TRIGGER_VIEWPORT,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
    MetadataRefreshRequest,
    build_refresh_request,
    metadata_refresh_due,
    refresh_requests_from_payload,
)
from trakt_tracker.config import AppConfig
from trakt_tracker.domain import ExploreResultPage, TitleSummary, TitleType
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient
from trakt_tracker.infrastructure.tmdb import TMDbClient
from trakt_tracker.infrastructure.url_utils import normalize_external_url


_WATCHLIST_SNAPSHOT_STATE_KEY = "watchlist_snapshot_v1"
_EXPLORE_SNAPSHOT_STATE_KEY = "explore_snapshots_v1"


def _serialize_title_summary(title: TitleSummary) -> dict:
    payload = asdict(title)
    for key in ("poster_refreshed_at", "backdrop_refreshed_at", "ratings_refreshed_at", "released_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    return payload


def _parse_optional_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _deserialize_title_summary(payload: object) -> TitleSummary | None:
    if not isinstance(payload, dict):
        return None
    try:
        title = TitleSummary(**payload)
    except (TypeError, ValueError):
        return None
    title.poster_refreshed_at = _parse_optional_datetime(title.poster_refreshed_at)
    title.backdrop_refreshed_at = _parse_optional_datetime(title.backdrop_refreshed_at)
    title.ratings_refreshed_at = _parse_optional_datetime(title.ratings_refreshed_at)
    title.released_at = _parse_optional_datetime(title.released_at)
    return title


class CatalogService:
    def __init__(
        self,
        db,
        auth_service,
        titles,
        user_states,
        sync_state,
        tmdb_factory: Callable[[AppConfig], TMDbClient],
        imdb_client: IMDbDatasetClient,
        history_repo=None,
        trakt_outbox=None,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles
        self._user_states = user_states
        self._sync_state = sync_state
        self._tmdb_factory = tmdb_factory
        self._imdb_client = imdb_client
        self._history_repo = history_repo
        self._trakt_outbox = trakt_outbox
        self._explore_cache: dict[tuple, dict] = {}
        self._explore_cache_lock = Lock()
        self._explore_refresh_locks: dict[tuple, Lock] = {}
        self._explore_snapshot_lock = Lock()

    def _normalize_title_refresh_requests(
        self,
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> tuple[MetadataRefreshRequest, ...]:
        if refresh_requests is not None:
            normalized = refresh_requests_from_payload(refresh_requests, allowed_parts=TITLE_ALLOWED_PARTS)
            if normalized:
                return normalized
        return (
            build_refresh_request(
                trigger=trigger,
                requested_parts=requested_parts,
                allowed_parts=TITLE_ALLOWED_PARTS,
            ),
        )

    @staticmethod
    def _title_row_type(record) -> str:
        if record is None:
            return ""
        if isinstance(record, dict):
            return str(record.get("type") or record.get("title_type") or "")
        return str(getattr(record, "title_type", "") or "")

    @staticmethod
    def _title_record_value(record, dict_key: str, attr_name: str):
        if record is None:
            return None
        if isinstance(record, dict):
            return record.get(dict_key)
        return getattr(record, attr_name, None)

    def _title_refresh_parts(
        self,
        record,
        title_type: str,
        refresh_requests: tuple[MetadataRefreshRequest, ...],
    ) -> dict[str, str]:
        if record is not None and self._title_row_type(record) not in {"", title_type}:
            return {}
        parts: dict[str, str] = {}
        can_enrich_posters = self._tmdb_factory(self._auth.config).is_configured()
        poster_url = str(self._title_record_value(record, "poster_url", "poster_url") or "")
        poster_status = str(
            self._title_record_value(record, "title_poster_status", "poster_status") or ENRICH_STATUS_UNKNOWN
        )
        poster_refreshed_at = self._title_record_value(record, "title_poster_refreshed_at", "poster_refreshed_at")
        backdrop_url = str(self._title_record_value(record, "backdrop_url", "backdrop_url") or "")
        backdrop_status = str(
            self._title_record_value(record, "title_backdrop_status", "backdrop_status") or ENRICH_STATUS_UNKNOWN
        )
        backdrop_refreshed_at = self._title_record_value(
            record,
            "title_backdrop_refreshed_at",
            "backdrop_refreshed_at",
        )
        ratings_status = str(
            self._title_record_value(record, "title_ratings_status", "ratings_status") or ENRICH_STATUS_UNKNOWN
        )
        ratings_refreshed_at = self._title_record_value(record, "title_ratings_refreshed_at", "ratings_refreshed_at")
        has_ratings_value = any(
            self._title_record_value(record, dict_key, attr_name) is not None
            for dict_key, attr_name in (
                ("title_trakt_rating", "trakt_rating"),
                ("title_imdb_rating", "imdb_rating"),
            )
        )
        for request in refresh_requests:
            requested = request.requested_parts or TITLE_ALLOWED_PARTS
            if ASSET_KIND_POSTER in requested and can_enrich_posters:
                poster_decision = metadata_refresh_due(
                    ASSET_KIND_POSTER,
                    status=poster_status,
                    last_checked_at=poster_refreshed_at,
                    has_value=bool(poster_url),
                    trigger=request.trigger,
                )
                if poster_decision.should_refresh:
                    parts[ASSET_KIND_POSTER] = poster_decision.reason
            if ASSET_KIND_BACKDROP in requested and title_type == "movie" and can_enrich_posters:
                backdrop_decision = metadata_refresh_due(
                    ASSET_KIND_BACKDROP,
                    status=backdrop_status,
                    last_checked_at=backdrop_refreshed_at,
                    has_value=bool(backdrop_url),
                    trigger=request.trigger,
                )
                if backdrop_decision.should_refresh:
                    parts[ASSET_KIND_BACKDROP] = backdrop_decision.reason
            if ASSET_KIND_TITLE_RATINGS in requested:
                ratings_decision = metadata_refresh_due(
                    ASSET_KIND_TITLE_RATINGS,
                    status=ratings_status,
                    last_checked_at=ratings_refreshed_at,
                    has_value=has_ratings_value,
                    trigger=request.trigger,
                )
                if ratings_decision.should_refresh:
                    parts[ASSET_KIND_TITLE_RATINGS] = ratings_decision.reason
        return parts

    def select_title_enrich_keys(
        self,
        rows: list[dict],
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> list[tuple[int, str]]:
        normalized_requests = self._normalize_title_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        result: list[tuple[int, str]] = []
        for row in rows:
            if not row.get("title_trakt_id") or row.get("type") not in {"movie", "show"}:
                continue
            if self._title_refresh_parts(row, str(row["type"]), normalized_requests):
                result.append((int(row["title_trakt_id"]), str(row["type"])))
        return list(dict.fromkeys(result))

    def search_titles(self, query: str, title_type: str | None = None) -> list[TitleSummary]:
        self._remember_search_query(query)
        client = self._auth.get_client()
        results = client.search_titles(query, title_type)
        results = self._merge_cached_title_metadata(results)
        results = self._normalize_title_urls(results)
        results = self._enrich_search_title_ratings(results)
        self.save_last_search_state(query, title_type, results)
        with self._db.session() as session:
            for title in results:
                self._titles.upsert_title(session, title)
                if title.ratings_status != ENRICH_STATUS_UNKNOWN:
                    self._titles.update_ratings_enrich_state(
                        session,
                        title.trakt_id,
                        status=title.ratings_status,
                        trakt_rating=title.trakt_rating,
                        trakt_votes=title.trakt_votes,
                        tmdb_id=title.tmdb_id,
                        tmdb_rating=title.tmdb_rating,
                        tmdb_votes=title.tmdb_votes,
                        imdb_id=title.imdb_id,
                        imdb_rating=title.imdb_rating,
                        imdb_votes=title.imdb_votes,
                    )
        return results

    def _search_title_page(
        self,
        query: str,
        title_type: str | None,
        *,
        page: int,
        limit: int,
    ) -> ExploreResultPage:
        result_page = self._auth.get_client().get_search_titles_page(
            query,
            title_type,
            page=page,
            limit=limit,
        )
        titles = self._merge_cached_title_metadata(result_page.items)
        titles = self._normalize_title_urls(titles)
        titles = self._enrich_search_title_ratings(titles)
        with self._db.session() as session:
            for title in titles:
                self._titles.upsert_title(session, title)
        return ExploreResultPage(items=titles, page=result_page.page, page_count=result_page.page_count)

    def filtered_search_titles(
        self,
        query: str,
        title_type: str | None,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        trakt_min: float | None,
        max_scan_pages: int,
        excluded_keys: set[tuple[str, int]] | None = None,
    ) -> ExploreResultPage:
        self._remember_search_query(query)
        exclusions = excluded_keys or set()
        if imdb_min is None and trakt_min is None and not exclusions:
            return self._search_title_page(
                query,
                title_type,
                page=page,
                limit=limit,
            )
        current_page = max(1, page)
        page_size = max(1, limit)
        scan_limit = max(1, max_scan_pages)
        cache_key = (
            "search",
            query.casefold(),
            title_type or "all",
            imdb_min,
            trakt_min,
            page_size,
            scan_limit,
            tuple(sorted(exclusions)),
        )
        now = time.monotonic()
        with self._explore_cache_lock:
            self._explore_cache = {
                key: value
                for key, value in self._explore_cache.items()
                if now - float(value["created_at"]) <= 120
            }
            entry = self._explore_cache.get(cache_key)
            if entry is None:
                entry = {"created_at": now, "items": [], "next_source_page": 1, "exhausted": False}
                self._explore_cache[cache_key] = entry
            target_count = current_page * page_size + 1
            while (
                len(entry["items"]) < target_count
                and not entry["exhausted"]
                and int(entry["next_source_page"]) <= scan_limit
            ):
                source_page_number = int(entry["next_source_page"])
                source_page = self._search_title_page(
                    query,
                    title_type,
                    page=source_page_number,
                    limit=page_size,
                )
                entry["items"].extend(
                    item
                    for item in source_page.items
                    if (
                        (imdb_min is None or (item.imdb_rating is not None and item.imdb_rating >= imdb_min))
                        and (trakt_min is None or (item.trakt_rating is not None and item.trakt_rating >= trakt_min))
                        and (item.title_type, int(item.trakt_id)) not in exclusions
                    )
                )
                entry["next_source_page"] = source_page_number + 1
                entry["exhausted"] = source_page_number >= source_page.page_count
            start = (current_page - 1) * page_size
            end = start + page_size
            items = list(entry["items"][start:end])
            has_next = len(entry["items"]) > end
        return ExploreResultPage(items=items, page=current_page, page_count=current_page + 1 if has_next else current_page)

    def watchlist_titles(self) -> list[TitleSummary]:
        titles = self._fetch_watchlist_titles()
        titles = self._merge_cached_title_metadata(titles)
        titles = self._normalize_title_urls(titles)
        titles = self._enrich_search_title_ratings(titles)
        with self._db.session() as session:
            for title in titles:
                self._titles.upsert_title(session, title)
        if self._trakt_outbox is not None:
            by_key = {(item.title_type, int(item.trakt_id)): item for item in titles}
            for payload, desired_member in self._trakt_outbox.membership_intents(operation_type="watchlist"):
                key = (str(payload.get("title_type") or ""), int(payload.get("trakt_id") or 0))
                if not desired_member:
                    by_key.pop(key, None)
                    continue
                if key not in by_key:
                    item = _deserialize_title_summary(payload.get("snapshot"))
                    if item is not None:
                        item.is_watchlisted = True
                        by_key[key] = item
            titles = list(by_key.values())
        self._save_watchlist_snapshot(
            {(title.title_type, int(title.trakt_id)) for title in titles},
            items=titles,
        )
        return titles

    def local_watchlist_titles(self) -> list[TitleSummary]:
        keys, available, items = self._load_watchlist_snapshot_payload()
        if not available:
            return []
        if not items and keys:
            with self._db.session() as session:
                stored = self._titles.by_trakt_ids(session, [trakt_id for _title_type, trakt_id in keys])
                items = [
                    self._summary_from_title_row(stored[trakt_id], is_watchlisted=True)
                    for title_type, trakt_id in sorted(keys)
                    if trakt_id in stored and stored[trakt_id].title_type == title_type
                ]
        items = self._merge_cached_title_metadata(items)
        for item in items:
            item.is_watchlisted = True
        return self._normalize_title_urls(items)

    def explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        trakt_min: float | None = None,
    ) -> ExploreResultPage:
        result_page = self._auth.get_client().get_explore_titles(
            title_type,
            feed,
            page=page,
            limit=limit,
            trakt_min=trakt_min,
        )
        titles = self._merge_cached_title_metadata(result_page.items)
        titles = self._normalize_title_urls(titles)
        titles = self._enrich_search_title_ratings(titles)
        with self._db.session() as session:
            for title in titles:
                self._titles.upsert_title(session, title)
        return ExploreResultPage(items=titles, page=result_page.page, page_count=result_page.page_count)

    def filtered_explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        trakt_min: float | None,
        max_scan_pages: int,
        excluded_keys: set[tuple[str, int]] | None = None,
    ) -> ExploreResultPage:
        exclusions = excluded_keys or set()
        if imdb_min is None and not exclusions:
            return self.explore_titles(
                title_type,
                feed,
                page=page,
                limit=limit,
                trakt_min=trakt_min,
            )
        current_page = max(1, page)
        page_size = max(1, limit)
        scan_limit = max(1, max_scan_pages)
        cache_key = (title_type, feed, imdb_min, trakt_min, page_size, scan_limit, tuple(sorted(exclusions)))
        now = time.monotonic()
        with self._explore_cache_lock:
            self._explore_cache = {
                key: value
                for key, value in self._explore_cache.items()
                if now - float(value["created_at"]) <= 120
            }
            entry = self._explore_cache.get(cache_key)
            if entry is None:
                entry = {
                    "created_at": now,
                    "items": [],
                    "next_source_page": 1,
                    "exhausted": False,
                }
                self._explore_cache[cache_key] = entry
                if len(self._explore_cache) > 8:
                    oldest_key = min(
                        self._explore_cache,
                        key=lambda key: float(self._explore_cache[key]["created_at"]),
                    )
                    if oldest_key != cache_key:
                        self._explore_cache.pop(oldest_key, None)
            refresh_lock = self._explore_refresh_locks.setdefault(cache_key, Lock())
        with refresh_lock:
            target_count = current_page * page_size + 1
            while (
                len(entry["items"]) < target_count
                and not entry["exhausted"]
                and int(entry["next_source_page"]) <= scan_limit
            ):
                source_page_number = int(entry["next_source_page"])
                source_page = self.explore_titles(
                    title_type,
                    feed,
                    page=source_page_number,
                    limit=page_size,
                    trakt_min=trakt_min,
                )
                entry["items"].extend(
                    item
                    for item in source_page.items
                    if (
                        (imdb_min is None or (item.imdb_rating is not None and item.imdb_rating >= imdb_min))
                        and (item.title_type, int(item.trakt_id)) not in exclusions
                    )
                )
                entry["next_source_page"] = source_page_number + 1
                entry["exhausted"] = source_page_number >= source_page.page_count
            start = (current_page - 1) * page_size
            end = start + page_size
            items = list(entry["items"][start:end])
            has_next = len(entry["items"]) > end
        return ExploreResultPage(
            items=items,
            page=current_page,
            page_count=current_page + 1 if has_next else current_page,
        )

    def local_explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        trakt_min: float | None,
        max_scan_pages: int,
        excluded_keys: set[tuple[str, int]] | None = None,
    ) -> ExploreResultPage | None:
        token = self._explore_snapshot_token(
            title_type,
            feed,
            page=page,
            limit=limit,
            imdb_min=imdb_min,
            trakt_min=trakt_min,
            max_scan_pages=max_scan_pages,
            excluded_keys=excluded_keys,
        )
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, _EXPLORE_SNAPSHOT_STATE_KEY, "")
        try:
            snapshots = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return None
        snapshot = snapshots.get(token) if isinstance(snapshots, dict) else None
        if not isinstance(snapshot, dict):
            return None
        items_raw = snapshot.get("items", [])
        if not isinstance(items_raw, list):
            return None
        items = [item for value in items_raw if (item := _deserialize_title_summary(value)) is not None]
        items = self._normalize_title_urls(self._merge_cached_title_metadata(items))
        try:
            snapshot_page = max(1, int(snapshot.get("page", page)))
            page_count = max(snapshot_page, int(snapshot.get("page_count", snapshot_page)))
        except (TypeError, ValueError):
            return None
        return ExploreResultPage(items=items, page=snapshot_page, page_count=page_count)

    def refresh_explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        trakt_min: float | None,
        max_scan_pages: int,
        excluded_keys: set[tuple[str, int]] | None = None,
    ) -> ExploreResultPage:
        result = self.filtered_explore_titles(
            title_type,
            feed,
            page=page,
            limit=limit,
            imdb_min=imdb_min,
            trakt_min=trakt_min,
            max_scan_pages=max_scan_pages,
            excluded_keys=excluded_keys,
        )
        token = self._explore_snapshot_token(
            title_type,
            feed,
            page=page,
            limit=limit,
            imdb_min=imdb_min,
            trakt_min=trakt_min,
            max_scan_pages=max_scan_pages,
            excluded_keys=excluded_keys,
        )
        with self._explore_snapshot_lock:
            with self._db.session() as session:
                raw = self._sync_state.get_value(session, _EXPLORE_SNAPSHOT_STATE_KEY, "")
            try:
                snapshots = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                snapshots = {}
            if not isinstance(snapshots, dict):
                snapshots = {}
            snapshots[token] = {
                "updated_at": time.time(),
                "page": result.page,
                "page_count": result.page_count,
                "items": [_serialize_title_summary(item) for item in result.items],
            }
            if len(snapshots) > 12:
                oldest = sorted(
                    snapshots,
                    key=lambda key: float(snapshots[key].get("updated_at", 0))
                    if isinstance(snapshots[key], dict)
                    else 0,
                )[:-12]
                for key in oldest:
                    snapshots.pop(key, None)
            with self._db.session() as session:
                self._sync_state.set_value(
                    session,
                    _EXPLORE_SNAPSHOT_STATE_KEY,
                    json.dumps(snapshots, ensure_ascii=False, separators=(",", ":")),
                )
        return result

    @staticmethod
    def _explore_snapshot_token(
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        trakt_min: float | None,
        max_scan_pages: int,
        excluded_keys: set[tuple[str, int]] | None,
    ) -> str:
        identity = json.dumps(
            [
                title_type,
                feed,
                max(1, int(page)),
                max(1, int(limit)),
                imdb_min,
                trakt_min,
                max(1, int(max_scan_pages)),
                sorted(excluded_keys or set()),
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def save_explore_rating_filters(
        self,
        imdb_min: str,
        trakt_min: str,
        *,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
        hide_releases: bool = False,
    ) -> None:
        values = {
            "imdb_min": imdb_min,
            "trakt_min": trakt_min,
            "hide_watchlisted": hide_watchlisted,
            "hide_history": hide_history,
            "hide_releases": hide_releases,
        }
        payload = json.dumps(values)
        with self._db.session() as session:
            self._sync_state.set_value(session, "explore_rating_filters", payload)

    def load_explore_rating_filters(self) -> dict:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "explore_rating_filters", "")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            return {"imdb_min": "", "trakt_min": "", "hide_watchlisted": False, "hide_history": False}
        result = {
            "imdb_min": str(payload.get("imdb_min", "") or ""),
            "trakt_min": str(payload.get("trakt_min", "") or ""),
            "hide_watchlisted": bool(payload.get("hide_watchlisted", False)),
            "hide_history": bool(payload.get("hide_history", False)),
            "hide_releases": bool(payload.get("hide_releases", False)),
        }
        return result

    def save_search_rating_filters(
        self,
        imdb_min: str,
        trakt_min: str,
        *,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
    ) -> None:
        payload = json.dumps(
            {
                "imdb_min": imdb_min,
                "trakt_min": trakt_min,
                "hide_watchlisted": hide_watchlisted,
                "hide_history": hide_history,
            }
        )
        with self._db.session() as session:
            self._sync_state.set_value(session, "search_rating_filters", payload)

    def load_search_rating_filters(self) -> dict:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "search_rating_filters", "")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "imdb_min": str(payload.get("imdb_min", "") or ""),
            "trakt_min": str(payload.get("trakt_min", "") or ""),
            "hide_watchlisted": bool(payload.get("hide_watchlisted", False)),
            "hide_history": bool(payload.get("hide_history", False)),
        }

    def has_watchlist_snapshot(self) -> bool:
        _keys, available = self._load_watchlist_snapshot()
        return available

    def refresh_watchlist_snapshot(self) -> None:
        self.watchlist_titles()

    def watchlist_keys(self, *, title_type: str | None = None) -> set[tuple[str, int]]:
        keys, _available = self._load_watchlist_snapshot()
        if title_type in {"movie", "show"}:
            return {key for key in keys if key[0] == title_type}
        return keys

    def history_keys(self) -> set[tuple[str, int]]:
        if self._history_repo is None:
            return set()
        with self._db.session() as session:
            return self._history_repo.watched_title_keys(session)

    def set_watchlisted(
        self,
        title_type: str,
        trakt_id: int,
        *,
        watchlisted: bool,
        snapshot: dict | None = None,
        dependency_key: str | None = None,
        origin: str = "user",
    ) -> None:
        if self._trakt_outbox is None:
            self._auth.get_client().set_watchlist(title_type, trakt_id, watchlisted=watchlisted)
            keys, available = self._load_watchlist_snapshot()
            if not available:
                self.refresh_watchlist_snapshot()
                return
            key = (title_type, int(trakt_id))
            if watchlisted:
                keys.add(key)
            else:
                keys.discard(key)
            self._save_watchlist_snapshot(keys)
            return

        keys, available = self._load_watchlist_snapshot()
        key = (title_type, int(trakt_id))
        base_member = key in keys if available else not watchlisted
        _old_keys, _old_available, items = self._load_watchlist_snapshot_payload()
        by_key = {(item.title_type, int(item.trakt_id)): item for item in items}
        if watchlisted:
            keys.add(key)
            compact = self._compact_watchlist_summary(title_type, int(trakt_id), snapshot or {})
            if compact is not None:
                by_key[key] = compact
        else:
            keys.discard(key)
            by_key.pop(key, None)
        queued = False
        with self._db.session() as session:
            self._save_watchlist_snapshot_in_session(session, keys, items=list(by_key.values()))
            operation_key = self._trakt_outbox.enqueue_membership(
                session,
                operation_type="watchlist",
                title_type=title_type,
                trakt_id=trakt_id,
                base_member=base_member,
                desired_member=watchlisted,
                snapshot=_serialize_title_summary(by_key[key]) if key in by_key else dict(snapshot or {}),
                dependency_key=dependency_key,
                origin=origin,
            )
            queued = operation_key is not None
        if queued:
            self._trakt_outbox.wake()

    def _fetch_watchlist_titles(self) -> list[TitleSummary]:
        fetch = self._auth.get_client().get_watchlist
        try:
            return fetch(authoritative=True)
        except TypeError as exc:
            if "authoritative" not in str(exc):
                raise
            return fetch()

    def _load_watchlist_snapshot(self) -> tuple[set[tuple[str, int]], bool]:
        keys, available, _items = self._load_watchlist_snapshot_payload()
        return keys, available

    def _load_watchlist_snapshot_payload(self) -> tuple[set[tuple[str, int]], bool, list[TitleSummary]]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, _WATCHLIST_SNAPSHOT_STATE_KEY, "")
        if not raw:
            return set(), False, []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return set(), False, []
        values = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return set(), False, []
        keys: set[tuple[str, int]] = set()
        for value in values:
            if not isinstance(value, list) or len(value) != 2:
                continue
            title_type = str(value[0] or "")
            try:
                trakt_id = int(value[1])
            except (TypeError, ValueError):
                continue
            if title_type in {"movie", "show"} and trakt_id > 0:
                keys.add((title_type, trakt_id))
        item_values = payload.get("items", []) if isinstance(payload, dict) else []
        items = [item for value in item_values if (item := _deserialize_title_summary(value)) is not None]
        items = [item for item in items if (item.title_type, int(item.trakt_id)) in keys]
        return keys, True, items

    def _save_watchlist_snapshot(
        self,
        keys: set[tuple[str, int]],
        *,
        items: list[TitleSummary] | None = None,
    ) -> None:
        if items is None:
            _old_keys, _available, existing_items = self._load_watchlist_snapshot_payload()
            items = [item for item in existing_items if (item.title_type, int(item.trakt_id)) in keys]
        with self._db.session() as session:
            self._save_watchlist_snapshot_in_session(session, keys, items=items)

    def _save_watchlist_snapshot_in_session(self, session, keys, *, items: list[TitleSummary]) -> None:
        payload = json.dumps(
            {
                "keys": [list(key) for key in sorted(keys)],
                "items": [_serialize_title_summary(item) for item in items],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._sync_state.set_value(session, _WATCHLIST_SNAPSHOT_STATE_KEY, payload)

    def _compact_watchlist_summary(
        self,
        title_type: str,
        trakt_id: int,
        snapshot: dict,
    ) -> TitleSummary | None:
        released_at = _parse_optional_datetime(snapshot.get("released_at"))
        title = str(snapshot.get("title") or "").strip()
        try:
            list_count = int(snapshot["list_count"]) if snapshot.get("list_count") not in {None, ""} else None
        except (TypeError, ValueError):
            list_count = None
        with self._db.session() as session:
            stored = self._titles.get_title(session, trakt_id)
            if stored is not None:
                summary = self._summary_from_title_row(stored, is_watchlisted=True)
                if title:
                    summary.title = title
                if released_at is not None:
                    summary.released_at = released_at
                if list_count is not None:
                    summary.explore_metric_kind = "lists"
                    summary.explore_metric_count = list_count
                return summary
        if not title:
            title = f"{'Show' if title_type == 'show' else 'Movie'} {trakt_id}"
        return TitleSummary(
            trakt_id=trakt_id,
            title_type="show" if title_type == "show" else "movie",
            title=title,
            released_at=released_at,
            explore_metric_kind="lists" if list_count is not None else "",
            explore_metric_count=list_count,
            is_watchlisted=True,
        )

    @staticmethod
    def _summary_from_title_row(row, *, is_watchlisted: bool = False) -> TitleSummary:
        title_type: TitleType = "movie" if row.title_type == "movie" else "show"
        return TitleSummary(
            trakt_id=int(row.trakt_id),
            title_type=title_type,
            title=str(row.title or ""),
            year=row.year,
            overview=str(row.overview or ""),
            poster_url=str(row.poster_url or ""),
            backdrop_url=str(row.backdrop_url or ""),
            status=str(row.status or ""),
            slug=str(row.slug or ""),
            trakt_rating=row.trakt_rating,
            trakt_votes=row.trakt_votes,
            tmdb_id=row.tmdb_id,
            tmdb_rating=row.tmdb_rating,
            tmdb_votes=row.tmdb_votes,
            imdb_id=str(row.imdb_id or ""),
            imdb_rating=row.imdb_rating,
            imdb_votes=row.imdb_votes,
            ratings_status=str(row.ratings_status or ENRICH_STATUS_UNKNOWN),
            ratings_refreshed_at=row.ratings_refreshed_at,
            poster_refreshed_at=row.poster_refreshed_at,
            backdrop_refreshed_at=row.backdrop_refreshed_at,
            is_watchlisted=is_watchlisted,
        )

    @staticmethod
    def _normalize_title_urls(titles: list[TitleSummary]) -> list[TitleSummary]:
        for title in titles:
            title.poster_url = normalize_external_url(title.poster_url)
            title.backdrop_url = normalize_external_url(title.backdrop_url)
        return titles

    def _enrich_search_title_ratings(self, titles: list[TitleSummary]) -> list[TitleSummary]:
        if not titles or not self._imdb_client.is_ready():
            return titles
        enriched: list[TitleSummary] = []
        for title in titles:
            if title.imdb_rating is not None and title.imdb_votes is not None:
                title.ratings_status = ENRICH_STATUS_READY
                enriched.append(title)
                continue
            if title.imdb_id:
                title = self._imdb_client.enrich_title(title)
                title.ratings_status = (
                    ENRICH_STATUS_READY
                    if title.imdb_rating is not None and title.imdb_votes is not None
                    else ENRICH_STATUS_CHECKED_NO_DATA
                )
            else:
                title.ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
            enriched.append(title)
        return enriched

    def enrich_title_with_tmdb(self, title: TitleSummary) -> TitleSummary:
        tmdb = self._tmdb_factory(self._auth.config)
        enriched = self._merge_cached_title_metadata([title])[0]
        if tmdb.is_configured():
            enriched = tmdb.enrich_title(enriched)
        ratings_status = ENRICH_STATUS_UNKNOWN
        if self._imdb_client.is_ready():
            if enriched.imdb_id:
                enriched = self._imdb_client.enrich_title(enriched)
                ratings_status = (
                    ENRICH_STATUS_READY
                    if enriched.imdb_rating is not None and enriched.imdb_votes is not None
                    else ENRICH_STATUS_CHECKED_NO_DATA
                )
            else:
                ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
        enriched = self._merge_cached_title_metadata([enriched])[0]
        if ratings_status != ENRICH_STATUS_UNKNOWN:
            enriched.ratings_status = ratings_status
        with self._db.session() as session:
            self._titles.upsert_title(session, enriched)
            if ratings_status != ENRICH_STATUS_UNKNOWN:
                self._titles.update_ratings_enrich_state(
                    session,
                    enriched.trakt_id,
                    status=ratings_status,
                    trakt_rating=enriched.trakt_rating,
                    trakt_votes=enriched.trakt_votes,
                    tmdb_id=enriched.tmdb_id,
                    tmdb_rating=enriched.tmdb_rating,
                    tmdb_votes=enriched.tmdb_votes,
                    imdb_id=enriched.imdb_id,
                    imdb_rating=enriched.imdb_rating,
                    imdb_votes=enriched.imdb_votes,
                )
        return enriched

    def _merge_cached_title_metadata(self, titles: list[TitleSummary]) -> list[TitleSummary]:
        if not titles:
            return []
        merged: list[TitleSummary] = []
        with self._db.session() as session:
            for title in titles:
                stored = self._titles.get_title(session, int(title.trakt_id))
                if stored is None:
                    merged.append(title)
                    continue
                if stored.poster_url:
                    title.poster_url = normalize_external_url(str(stored.poster_url or ""))
                if stored.backdrop_url:
                    title.backdrop_url = normalize_external_url(str(stored.backdrop_url or ""))
                if not title.status and stored.status:
                    title.status = str(stored.status or "")
                if not title.slug and stored.slug:
                    title.slug = str(stored.slug or "")
                if title.trakt_rating is None and stored.trakt_rating is not None:
                    title.trakt_rating = stored.trakt_rating
                if title.trakt_votes is None and stored.trakt_votes is not None:
                    title.trakt_votes = stored.trakt_votes
                if title.tmdb_id is None and stored.tmdb_id is not None:
                    title.tmdb_id = stored.tmdb_id
                if title.tmdb_rating is None and stored.tmdb_rating is not None:
                    title.tmdb_rating = stored.tmdb_rating
                if title.tmdb_votes is None and stored.tmdb_votes is not None:
                    title.tmdb_votes = stored.tmdb_votes
                if not title.imdb_id and stored.imdb_id:
                    title.imdb_id = str(stored.imdb_id or "")
                if title.imdb_rating is None and stored.imdb_rating is not None:
                    title.imdb_rating = stored.imdb_rating
                if title.imdb_votes is None and stored.imdb_votes is not None:
                    title.imdb_votes = stored.imdb_votes
                if stored.ratings_status:
                    title.ratings_status = str(stored.ratings_status)
                if stored.ratings_refreshed_at is not None:
                    title.ratings_refreshed_at = stored.ratings_refreshed_at
                merged.append(title)
        return merged

    def save_last_search_state(
        self,
        query: str,
        title_type: str | None,
        results: list[TitleSummary],
        *,
        imdb_min: str = "",
        trakt_min: str = "",
    ) -> None:
        payload = {
            "query": query,
            "title_type": title_type or "all",
            "sort_mode": self.get_search_sort_mode(),
            "imdb_min": imdb_min,
            "trakt_min": trakt_min,
            "results": [_serialize_title_summary(item) for item in results],
        }
        with self._db.session() as session:
            self._sync_state.set_value(session, "last_search_state", json.dumps(payload, ensure_ascii=False))

    def load_last_search_state(self) -> dict | None:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "last_search_state", "")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        results_raw = payload.get("results", [])
        if not isinstance(results_raw, list):
            results_raw = []
        results: list[TitleSummary] = []
        for item in results_raw:
            if not isinstance(item, dict):
                continue
            try:
                title = TitleSummary(**item)
            except TypeError:
                continue
            title.poster_refreshed_at = _parse_optional_datetime(title.poster_refreshed_at)
            title.backdrop_refreshed_at = _parse_optional_datetime(title.backdrop_refreshed_at)
            title.ratings_refreshed_at = _parse_optional_datetime(title.ratings_refreshed_at)
            title.released_at = _parse_optional_datetime(title.released_at)
            title.poster_url = normalize_external_url(title.poster_url)
            results.append(title)
        results = self._merge_cached_title_metadata(results)
        return {
            "query": str(payload.get("query", "") or ""),
            "title_type": str(payload.get("title_type", "all") or "all"),
            "sort_mode": str(payload.get("sort_mode", "IMDb votes") or "IMDb votes"),
            "imdb_min": str(payload.get("imdb_min", "") or ""),
            "trakt_min": str(payload.get("trakt_min", "") or ""),
            "results": results,
        }

    def search_history(self) -> list[str]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "search_history", "[]")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, str) and item.strip()]

    def get_title_details(
        self,
        trakt_id: int,
        title_type: str,
        *,
        use_cache: bool = True,
        refresh_posters: bool = True,
        refresh_backdrops: bool = True,
        refresh_ratings: bool = True,
    ) -> TitleSummary:
        client = self._auth.get_client()
        title = client.get_title_details(trakt_id, title_type, use_cache=use_cache)
        title = self._merge_cached_title_metadata([title])[0]
        poster_status = ENRICH_STATUS_UNKNOWN
        backdrop_status = ENRICH_STATUS_UNKNOWN
        ratings_status = ENRICH_STATUS_UNKNOWN
        tmdb = self._tmdb_factory(self._auth.config)
        tmdb_error = False
        if (refresh_posters or refresh_backdrops) and tmdb.is_configured():
            if title.tmdb_id is None:
                if refresh_posters and not title.poster_url:
                    poster_status = ENRICH_STATUS_CHECKED_NO_DATA
                if refresh_backdrops and title_type == "movie":
                    backdrop_status = ENRICH_STATUS_CHECKED_NO_DATA
            elif (refresh_posters and not title.poster_url) or (refresh_backdrops and title_type == "movie"):
                try:
                    title = tmdb.enrich_title(title)
                except Exception:
                    tmdb_error = True
        if refresh_posters:
            if tmdb_error and not title.poster_url:
                poster_status = ENRICH_STATUS_RETRYABLE_FAILURE
            else:
                poster_status = ENRICH_STATUS_READY if title.poster_url else ENRICH_STATUS_CHECKED_NO_DATA
        if refresh_backdrops and title_type == "movie":
            if tmdb_error and not title.backdrop_url:
                backdrop_status = ENRICH_STATUS_RETRYABLE_FAILURE
            else:
                backdrop_status = ENRICH_STATUS_READY if title.backdrop_url else ENRICH_STATUS_CHECKED_NO_DATA
        if refresh_ratings and self._imdb_client.is_ready():
            if title.imdb_id:
                title = self._imdb_client.enrich_title(title)
                ratings_status = (
                    ENRICH_STATUS_READY
                    if title.imdb_rating is not None and title.imdb_votes is not None
                    else ENRICH_STATUS_CHECKED_NO_DATA
                )
            else:
                ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
        with self._db.session() as session:
            model = self._titles.upsert_title(session, title)
            if refresh_posters:
                self._titles.update_poster_enrich_state(
                    session,
                    trakt_id,
                    status=poster_status,
                    poster_url=title.poster_url,
                )
            if refresh_backdrops and title_type == "movie":
                self._titles.update_backdrop_enrich_state(
                    session,
                    trakt_id,
                    status=backdrop_status,
                    backdrop_url=title.backdrop_url,
                )
            if refresh_ratings:
                self._titles.update_ratings_enrich_state(
                    session,
                    trakt_id,
                    status=ratings_status,
                    trakt_rating=title.trakt_rating,
                    trakt_votes=title.trakt_votes,
                    tmdb_id=title.tmdb_id,
                    tmdb_rating=title.tmdb_rating,
                    tmdb_votes=title.tmdb_votes,
                    imdb_id=title.imdb_id,
                    imdb_rating=title.imdb_rating,
                    imdb_votes=title.imdb_votes,
                )
            self._user_states.ensure_state(session, model.id)
        return title

    def enrich_visible_titles(self, rows: list[dict]) -> bool:
        title_items = self.select_title_enrich_keys(rows)
        if not title_items:
            return False
        changed = False
        for trakt_id, title_type in dict.fromkeys(title_items):
            result = self.enrich_title_key(trakt_id, title_type)
            if result == ENRICH_STATUS_READY:
                changed = True
        return changed

    def has_missing_visible_titles(self, rows: list[dict]) -> bool:
        return bool(self.select_title_enrich_keys(rows))

    def title_key_needs_enrich(
        self,
        trakt_id: int,
        title_type: str,
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> bool:
        with self._db.session() as session:
            row = self._titles.get_title(session, trakt_id)
        normalized_requests = self._normalize_title_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        return bool(self._title_refresh_parts(row, title_type, normalized_requests))

    def enrich_title_key(
        self,
        trakt_id: int,
        title_type: str,
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> str:
        normalized_requests = self._normalize_title_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        if not self.title_key_needs_enrich(
            trakt_id,
            title_type,
            refresh_requests=tuple(request.to_payload() for request in normalized_requests),
        ):
            return TASK_RESULT_SKIPPED_ALREADY_RESOLVED
        due_parts: dict[str, str] = {}
        with self._db.session() as session:
            row = self._titles.get_title(session, trakt_id)
            due_parts = self._title_refresh_parts(row, title_type, normalized_requests)
        refresh_posters = ASSET_KIND_POSTER in due_parts
        refresh_backdrops = ASSET_KIND_BACKDROP in due_parts
        refresh_ratings = ASSET_KIND_TITLE_RATINGS in due_parts
        force_network = refresh_ratings or any(
            request.trigger in {TRIGGER_VISIBLE_RATINGS_REFRESH, TRIGGER_SYNC_EVENT, TRIGGER_MANUAL_REPAIR}
            for request in normalized_requests
        )
        try:
            self.get_title_details(
                trakt_id,
                title_type,
                use_cache=not force_network,
                refresh_posters=refresh_posters,
                refresh_backdrops=refresh_backdrops,
                refresh_ratings=refresh_ratings,
            )
        except Exception:
            with self._db.session() as session:
                row = self._titles.get_title(session, trakt_id)
                if row is not None:
                    if refresh_posters:
                        self._titles.update_poster_enrich_state(session, trakt_id, status=ENRICH_STATUS_RETRYABLE_FAILURE)
                    if refresh_backdrops:
                        self._titles.update_backdrop_enrich_state(session, trakt_id, status=ENRICH_STATUS_RETRYABLE_FAILURE)
                    if refresh_ratings:
                        self._titles.update_ratings_enrich_state(session, trakt_id, status=ENRICH_STATUS_RETRYABLE_FAILURE)
            return ENRICH_STATUS_RETRYABLE_FAILURE
        with self._db.session() as session:
            row = self._titles.get_title(session, trakt_id)
            if row is None:
                return ENRICH_STATUS_RETRYABLE_FAILURE
            remaining_due = self._title_refresh_parts(row, title_type, normalized_requests)
            if remaining_due:
                return ENRICH_STATUS_RETRYABLE_FAILURE
            if (
                (refresh_posters and row.poster_url)
                or (refresh_backdrops and row.backdrop_url)
                or (refresh_ratings and (row.trakt_rating is not None or row.imdb_rating is not None))
                or (
                    not refresh_posters
                    and not refresh_backdrops
                    and not refresh_ratings
                    and (row.poster_url or row.backdrop_url or row.trakt_rating is not None or row.imdb_rating is not None)
                )
            ):
                return ENRICH_STATUS_READY
        return ENRICH_STATUS_CHECKED_NO_DATA

    def get_search_sort_mode(self) -> str:
        with self._db.session() as session:
            return self._sync_state.get_value(session, "search_sort_mode", "IMDb votes")

    def set_search_sort_mode(self, mode: str) -> None:
        with self._db.session() as session:
            self._sync_state.set_value(session, "search_sort_mode", mode)

    def remember_search_query(self, query: str) -> None:
        self._remember_search_query(query)

    def _remember_search_query(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "search_history", "[]")
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
            if not isinstance(items, list):
                items = []
            deduped = [item for item in items if isinstance(item, str) and item.strip() and item != query]
            deduped.insert(0, query)
            self._sync_state.set_value(session, "search_history", json.dumps(deduped[:15], ensure_ascii=False))
