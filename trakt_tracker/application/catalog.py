from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Callable

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.application.enrich_queue import TASK_RESULT_SKIPPED_ALREADY_RESOLVED
from trakt_tracker.application.metadata_refresh_policy import (
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
from trakt_tracker.domain import TitleSummary
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient
from trakt_tracker.infrastructure.tmdb import TMDbClient
from trakt_tracker.infrastructure.url_utils import normalize_external_url


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
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles
        self._user_states = user_states
        self._sync_state = sync_state
        self._tmdb_factory = tmdb_factory
        self._imdb_client = imdb_client

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
        self.save_last_search_state(query, title_type, results)
        with self._db.session() as session:
            for title in results:
                self._titles.upsert_title(session, title)
        return results

    def enrich_title_with_tmdb(self, title: TitleSummary) -> TitleSummary:
        tmdb = self._tmdb_factory(self._auth.config)
        enriched = title
        if tmdb.is_configured():
            enriched = tmdb.enrich_title(enriched)
        enriched = self._imdb_client.enrich_title(enriched)
        with self._db.session() as session:
            self._titles.upsert_title(session, enriched)
        return enriched

    def save_last_search_state(self, query: str, title_type: str | None, results: list[TitleSummary]) -> None:
        payload = {
            "query": query,
            "title_type": title_type or "all",
            "sort_mode": self.get_search_sort_mode(),
            "results": [asdict(item) for item in results],
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
            title.poster_url = normalize_external_url(title.poster_url)
            results.append(title)
        return {
            "query": str(payload.get("query", "") or ""),
            "title_type": str(payload.get("title_type", "all") or "all"),
            "sort_mode": str(payload.get("sort_mode", "IMDb votes") or "IMDb votes"),
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
        refresh_ratings: bool = True,
    ) -> TitleSummary:
        client = self._auth.get_client()
        title = client.get_title_details(trakt_id, title_type, use_cache=use_cache)
        poster_status = ENRICH_STATUS_UNKNOWN
        ratings_status = ENRICH_STATUS_UNKNOWN
        tmdb = self._tmdb_factory(self._auth.config)
        if title.poster_url:
            poster_status = ENRICH_STATUS_READY
        elif refresh_posters and tmdb.is_configured():
            if title.tmdb_id is None:
                poster_status = ENRICH_STATUS_CHECKED_NO_DATA
            else:
                try:
                    title = tmdb.enrich_title(title)
                except Exception:
                    poster_status = ENRICH_STATUS_RETRYABLE_FAILURE
                else:
                    poster_status = ENRICH_STATUS_READY if title.poster_url else ENRICH_STATUS_CHECKED_NO_DATA
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
                refresh_ratings=refresh_ratings,
            )
        except Exception:
            with self._db.session() as session:
                row = self._titles.get_title(session, trakt_id)
                if row is not None:
                    if refresh_posters:
                        self._titles.update_poster_enrich_state(session, trakt_id, status=ENRICH_STATUS_RETRYABLE_FAILURE)
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
                or (refresh_ratings and (row.trakt_rating is not None or row.imdb_rating is not None))
                or (not refresh_posters and not refresh_ratings and (row.poster_url or row.trakt_rating is not None or row.imdb_rating is not None))
            ):
                return ENRICH_STATUS_READY
        return ENRICH_STATUS_CHECKED_NO_DATA

    def get_search_sort_mode(self) -> str:
        with self._db.session() as session:
            return self._sync_state.get_value(session, "search_sort_mode", "IMDb votes")

    def set_search_sort_mode(self, mode: str) -> None:
        with self._db.session() as session:
            self._sync_state.set_value(session, "search_sort_mode", mode)

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
