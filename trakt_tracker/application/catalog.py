from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
    should_attempt_enrich,
)
from trakt_tracker.application.enrich_queue import TASK_RESULT_SKIPPED_ALREADY_RESOLVED
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

    def _visible_title_items_needing_enrich(self, rows: list[dict]) -> list[tuple[int, str]]:
        can_enrich_posters = self._tmdb_factory(self._auth.config).is_configured()
        return [
            (int(row["title_trakt_id"]), str(row["type"]))
            for row in rows
            if row.get("title_trakt_id") and row.get("type") in {"movie", "show"} and (
                (
                    can_enrich_posters
                    and should_attempt_enrich(
                        row.get("title_poster_status", ENRICH_STATUS_UNKNOWN),
                        has_value=bool(row.get("poster_url")) and row.get("title_poster_status") == ENRICH_STATUS_READY,
                    )
                )
                or row.get("title_ratings_status", ENRICH_STATUS_UNKNOWN) in {ENRICH_STATUS_UNKNOWN, ENRICH_STATUS_RETRYABLE_FAILURE}
            )
        ]

    def select_title_enrich_keys(self, rows: list[dict]) -> list[tuple[int, str]]:
        return list(dict.fromkeys(self._visible_title_items_needing_enrich(rows)))

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

    def get_title_details(self, trakt_id: int, title_type: str) -> TitleSummary:
        client = self._auth.get_client()
        title = client.get_title_details(trakt_id, title_type)
        poster_status = ENRICH_STATUS_UNKNOWN
        ratings_status = ENRICH_STATUS_UNKNOWN
        tmdb = self._tmdb_factory(self._auth.config)
        if title.poster_url:
            poster_status = ENRICH_STATUS_READY
        elif tmdb.is_configured():
            if title.tmdb_id is None:
                poster_status = ENRICH_STATUS_CHECKED_NO_DATA
            else:
                try:
                    title = tmdb.enrich_title(title)
                except Exception:
                    poster_status = ENRICH_STATUS_RETRYABLE_FAILURE
                else:
                    poster_status = ENRICH_STATUS_READY if title.poster_url else ENRICH_STATUS_CHECKED_NO_DATA
        if self._imdb_client.is_ready():
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
            self._titles.update_poster_enrich_state(
                session,
                trakt_id,
                status=poster_status,
                poster_url=title.poster_url,
            )
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

    def title_key_needs_enrich(self, trakt_id: int, title_type: str) -> bool:
        with self._db.session() as session:
            row = self._titles.get_title(session, trakt_id)
        return self._title_row_needs_enrich(row, title_type)

    def _title_row_needs_enrich(self, row, title_type: str) -> bool:
        if row is None or row.title_type != title_type:
            return True
        needs_poster = self._tmdb_factory(self._auth.config).is_configured() and should_attempt_enrich(
            getattr(row, "poster_status", ENRICH_STATUS_UNKNOWN),
            has_value=bool(getattr(row, "poster_url", "")) and getattr(row, "poster_status", ENRICH_STATUS_UNKNOWN) == ENRICH_STATUS_READY,
        )
        needs_ratings = getattr(row, "ratings_status", ENRICH_STATUS_UNKNOWN) in {ENRICH_STATUS_UNKNOWN, ENRICH_STATUS_RETRYABLE_FAILURE}
        return bool(needs_poster or needs_ratings)

    def enrich_title_key(self, trakt_id: int, title_type: str) -> str:
        if not self.title_key_needs_enrich(trakt_id, title_type):
            return TASK_RESULT_SKIPPED_ALREADY_RESOLVED
        try:
            self.get_title_details(trakt_id, title_type)
        except Exception:
            with self._db.session() as session:
                row = self._titles.get_title(session, trakt_id)
                if row is not None:
                    if should_attempt_enrich(
                        getattr(row, "poster_status", ENRICH_STATUS_UNKNOWN),
                        has_value=bool(getattr(row, "poster_url", "")) and getattr(row, "poster_status", ENRICH_STATUS_UNKNOWN) == ENRICH_STATUS_READY,
                    ):
                        self._titles.update_poster_enrich_state(session, trakt_id, status=ENRICH_STATUS_RETRYABLE_FAILURE)
                    if getattr(row, "ratings_status", ENRICH_STATUS_UNKNOWN) in {ENRICH_STATUS_UNKNOWN, ENRICH_STATUS_RETRYABLE_FAILURE}:
                        self._titles.update_ratings_enrich_state(session, trakt_id, status=ENRICH_STATUS_RETRYABLE_FAILURE)
            return ENRICH_STATUS_RETRYABLE_FAILURE
        with self._db.session() as session:
            row = self._titles.get_title(session, trakt_id)
            if row is None:
                return ENRICH_STATUS_RETRYABLE_FAILURE
            if self._title_row_needs_enrich(row, title_type):
                return ENRICH_STATUS_RETRYABLE_FAILURE
            if row.poster_url or row.trakt_rating is not None or row.imdb_rating is not None:
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
