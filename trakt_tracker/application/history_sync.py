from __future__ import annotations

import json
from datetime import UTC, datetime

from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.application.trakt_payload_cache import (
    load_cached_trakt_history_items,
    load_cached_trakt_rating_items,
)
from trakt_tracker.domain import DashboardState, EpisodeSummary, TitleSummary
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient


class HistorySyncWorkflow:
    def __init__(
        self,
        db,
        auth_service,
        titles,
        user_states,
        history,
        progress,
        episode_repo,
        sync_state,
        imdb_client: IMDbDatasetClient,
        operations: OperationLog,
        episode_metadata: EpisodeMetadataService,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles
        self._user_states = user_states
        self._history = history
        self._progress = progress
        self._episode_repo = episode_repo
        self._sync_state = sync_state
        self._imdb_client = imdb_client
        self._operations = operations
        self._policy = SyncPolicy
        self._episode_metadata = episode_metadata

    def initial_import(self) -> None:
        client = self._auth.get_client()
        history_items = self._fetch_all_watch_history(client)
        ratings = self._fetch_all_ratings(client)
        self._sync_history_and_ratings(history_items, ratings)

    def refresh_history(self) -> None:
        self.sync_updates()

    def maybe_refresh_history(self) -> bool:
        if not self._auth.is_authorized():
            self._operations.publish("History auto-sync", "Skipped: Trakt is not authorized.")
            return False
        with self._db.session() as session:
            last_probe_at_raw = self._sync_state.get_value(session, SyncPolicy.HISTORY_PROBE_KEY)
            previous_signature = self._sync_state.get_value(session, SyncPolicy.HISTORY_SIGNATURE_KEY)
            last_sync_at_raw = self._sync_state.get_value(session, SyncPolicy.HISTORY_LAST_SYNC_KEY)
        if not self._policy.should_probe_history(last_probe_at_raw):
            self._operations.publish("History auto-sync", "Skipped: probe interval has not elapsed yet.")
            return False
        signature = self._current_history_activity_signature()
        with self._db.session() as session:
            self._sync_state.set_value(session, SyncPolicy.HISTORY_PROBE_KEY, datetime.now(tz=UTC).isoformat())
        if not signature:
            self._operations.publish("History auto-sync", "Skipped: no activity signature received.")
            return False
        if not self._policy.should_run_history_sync(
            current_signature=signature,
            previous_signature=previous_signature,
            last_sync_at_raw=last_sync_at_raw,
        ):
            self._operations.publish("History auto-sync", "Skipped: no relevant Trakt history/rating changes.")
            return False
        self._operations.publish("History auto-sync", "Changes detected; running history sync.")
        self.sync_updates()
        return True

    def sync_updates(self) -> None:
        self._operations.publish("History sync", "Fetching recent history updates and ratings.")
        client = self._auth.get_client()
        client.clear_cache()
        history_items = self._fetch_recent_history_updates(client)
        ratings = self._fetch_all_ratings(client)
        self._sync_history_and_ratings(history_items, ratings)
        signature = self._current_history_activity_signature()
        if signature:
            with self._db.session() as session:
                self._sync_state.set_value(session, SyncPolicy.HISTORY_SIGNATURE_KEY, signature)
                self._sync_state.set_value(session, SyncPolicy.HISTORY_LAST_SYNC_KEY, datetime.now(tz=UTC).isoformat())
        self._operations.publish("History sync", f"Imported {len(history_items)} history item(s) and {len(ratings)} rating item(s).")

    def repair_legacy_episode_history(self) -> bool:
        with self._db.session() as session:
            legacy_count = len([row for row in self._history.list_filtered(session, limit=500) if row.title_type == "episode"])
        if legacy_count == 0:
            return False
        history_items = load_cached_trakt_history_items()
        if not history_items:
            client = self._auth.get_client()
            history_items = self._fetch_all_watch_history(client)
        rating_items = load_cached_trakt_rating_items()
        if not rating_items:
            client = self._auth.get_client()
            rating_items = self._fetch_all_ratings(client)
        show_ids: set[int] = set()
        with self._db.session() as session:
            for item in history_items:
                imported = self._import_history_item(session, item)
                if imported is not None and imported["title_type"] == "show":
                    show_ids.add(imported["trakt_id"])
            self._history.delete_trakt_rated(session)
            for item in rating_items:
                self._import_rating_item(session, item)
        for trakt_id in show_ids:
            self.refresh_show(trakt_id)
        self._episode_metadata.backfill_episode_imdb_ids_from_payloads(history_items + rating_items)
        self._episode_metadata.enrich_episode_imdb_ratings()
        return True

    def refresh_show(self, trakt_id: int):
        client = self._auth.get_client()
        progress = client.get_show_progress(trakt_id)
        episodes = client.get_show_episodes(trakt_id)
        with self._db.session() as session:
            self._progress.upsert_progress(session, progress)
            self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
        return progress

    def dashboard_state(self) -> DashboardState:
        with self._db.session() as session:
            return DashboardState(
                in_progress=self._progress.list_in_progress(session),
                recent_history=[
                    {
                        "title": item.title,
                        "type": item.title_type,
                        "action": item.action,
                        "watched_at": item.watched_at,
                    }
                    for item in self._history.list_recent(session)
                ],
                upcoming=self._episode_repo.list_upcoming(session),
            )

    def _sync_history_and_ratings(self, history_items: list[dict], ratings: list[dict]) -> None:
        show_ids: set[int] = set()
        with self._db.session() as session:
            for item in history_items:
                imported = self._import_history_item(session, item)
                if imported is not None and imported["title_type"] == "show":
                    show_ids.add(imported["trakt_id"])
            self._history.delete_trakt_rated(session)
            for item in ratings:
                self._import_rating_item(session, item)
            self._sync_state.set_value(session, "initial_import_at", datetime.now(tz=UTC).isoformat())
        for trakt_id in show_ids:
            self.refresh_show(trakt_id)
        self._episode_metadata.backfill_episode_imdb_ids_from_payloads(history_items + ratings)
        self._episode_metadata.enrich_episode_imdb_ratings()

    def _import_history_item(self, session, item: dict) -> dict | None:
        raw_type = item.get("type")
        season = None
        episode_number = None
        if raw_type == "episode":
            episode_payload = item.get("episode", {}) or {}
            show_payload = item.get("show", {}) or {}
            ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
            trakt_id = ids.get("trakt")
            if not trakt_id:
                return None
            title_type = "show"
            title = TitleSummary(
                trakt_id=trakt_id,
                title_type="show",
                title=show_payload.get("title", ""),
                year=show_payload.get("year"),
                overview=show_payload.get("overview", ""),
                status=show_payload.get("status", ""),
                slug=ids.get("slug", ""),
            )
            season = episode_payload.get("season")
            episode_number = episode_payload.get("number")
            episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
            episode_trakt_id = episode_ids.get("trakt", 0)
            if season is not None and episode_number is not None:
                self._episode_repo.upsert_episode(
                    session,
                    trakt_id,
                    EpisodeSummary(
                        trakt_id=episode_trakt_id,
                        season=season,
                        number=episode_number,
                        title=episode_payload.get("title", ""),
                        overview=episode_payload.get("overview", ""),
                        runtime=episode_payload.get("runtime"),
                        first_aired=(
                            datetime.fromisoformat(episode_payload["first_aired"].replace("Z", "+00:00"))
                            if episode_payload.get("first_aired")
                            else None
                        ),
                    ),
                )
        else:
            payload = item.get(raw_type, {})
            ids = payload.get("ids", {})
            trakt_id = ids.get("trakt")
            if not trakt_id:
                return None
            title_type = raw_type
            title = TitleSummary(
                trakt_id=trakt_id,
                title_type=title_type,
                title=payload.get("title", ""),
                year=payload.get("year"),
                overview=payload.get("overview", ""),
                status=payload.get("status", ""),
                slug=ids.get("slug", ""),
            )
        model = self._titles.upsert_title(session, title)
        state = self._user_states.ensure_state(session, model.id)
        state.in_history = True
        state.tracked = title_type == "show"
        watched_at_raw = item.get("watched_at")
        watched_at = datetime.fromisoformat(watched_at_raw.replace("Z", "+00:00")) if watched_at_raw else datetime.now(tz=UTC)
        state.last_watched_at = watched_at
        self._history.add_event(
            session,
            trakt_history_id=item.get("id"),
            title_trakt_id=trakt_id,
            title=title.title,
            title_type=title_type,
            action="watched",
            watched_at=watched_at,
            season=season,
            episode=episode_number,
            source="trakt",
        )
        return {"trakt_id": trakt_id, "title_type": title_type}

    def _import_rating_item(self, session, item: dict) -> None:
        raw_type = item.get("type")
        rating_value = item.get("rating")
        rated_at_raw = item.get("rated_at")
        rated_at = datetime.fromisoformat(rated_at_raw.replace("Z", "+00:00")) if rated_at_raw else datetime.now(tz=UTC)
        if raw_type == "episode":
            episode_payload = item.get("episode", {}) or {}
            show_payload = item.get("show", {}) or {}
            ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
            trakt_id = ids.get("trakt")
            if not trakt_id:
                return
            title = TitleSummary(
                trakt_id=trakt_id,
                title_type="show",
                title=show_payload.get("title", ""),
                year=show_payload.get("year"),
                overview=show_payload.get("overview", ""),
                status=show_payload.get("status", ""),
                slug=ids.get("slug", ""),
            )
            self._titles.upsert_title(session, title)
            season = episode_payload.get("season")
            episode_number = episode_payload.get("number")
            episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
            episode_trakt_id = episode_ids.get("trakt", 0)
            if season is not None and episode_number is not None:
                self._episode_repo.upsert_episode(
                    session,
                    trakt_id,
                    EpisodeSummary(
                        trakt_id=episode_trakt_id,
                        season=season,
                        number=episode_number,
                        title=episode_payload.get("title", ""),
                        overview=episode_payload.get("overview", ""),
                        runtime=episode_payload.get("runtime"),
                        first_aired=(
                            datetime.fromisoformat(episode_payload["first_aired"].replace("Z", "+00:00"))
                            if episode_payload.get("first_aired")
                            else None
                        ),
                    ),
                )
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=trakt_id,
                title=title.title,
                title_type="show",
                action="rated",
                watched_at=rated_at,
                season=season,
                episode=episode_number,
                rating=rating_value,
                source="trakt",
            )
            return
        payload = item.get(raw_type, {})
        ids = payload.get("ids", {})
        trakt_id = ids.get("trakt")
        if not trakt_id:
            return
        title = TitleSummary(
            trakt_id=trakt_id,
            title_type=raw_type,
            title=payload.get("title", ""),
            year=payload.get("year"),
            overview=payload.get("overview", ""),
            status=payload.get("status", ""),
            slug=ids.get("slug", ""),
        )
        model = self._titles.upsert_title(session, title)
        state = self._user_states.ensure_state(session, model.id)
        state.rating = rating_value
        self._history.add_event(
            session,
            trakt_history_id=None,
            title_trakt_id=trakt_id,
            title=title.title,
            title_type=raw_type,
            action="rated",
            watched_at=rated_at,
            rating=rating_value,
            source="trakt",
        )

    @staticmethod
    def _fetch_all_watch_history(client, page_size: int = 100) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = client.get_watch_history(limit=page_size, page=page)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return items

    @staticmethod
    def _fetch_all_ratings(client, page_size: int = 100) -> list[dict]:
        items: list[dict] = []
        seen_keys: set[tuple] = set()
        for title_type in ("episode", "show", "movie"):
            page = 1
            while True:
                batch = client.get_ratings(title_type=title_type, limit=page_size, page=page)
                if not batch:
                    break
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    key = (
                        item.get("rated_at"),
                        item.get("type"),
                        item.get("rating"),
                        ((item.get("show") or {}).get("ids") or {}).get("trakt"),
                        ((item.get("episode") or {}).get("ids") or {}).get("trakt"),
                        ((item.get("movie") or {}).get("ids") or {}).get("trakt"),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    items.append(item)
                if len(batch) < page_size:
                    break
                page += 1
        return items

    def _fetch_recent_history_updates(self, client, page_size: int = 100) -> list[dict]:
        with self._db.session() as session:
            known_ids = self._history.known_trakt_history_ids(session)
        if not known_ids:
            return self._fetch_all_watch_history(client, page_size=page_size)
        items: list[dict] = []
        page = 1
        while True:
            batch = client.get_watch_history(limit=page_size, page=page)
            if not batch:
                break
            unseen = [item for item in batch if item.get("id") not in known_ids]
            if unseen:
                items.extend(unseen)
            if not unseen or len(batch) < page_size:
                break
            page += 1
        return items

    def _current_history_activity_signature(self) -> str:
        client = self._auth.get_client()
        payload = client.get_last_activities(use_cache=False)
        return self._policy.build_history_activity_signature(payload)
