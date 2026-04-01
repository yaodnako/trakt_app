from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.config import AppConfig
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot, TitleSummary
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient
from trakt_tracker.infrastructure.tmdb import TMDbClient


class ProgressSyncWorkflow:
    def __init__(
        self,
        db,
        auth_service,
        progress_repo,
        episode_repo,
        titles,
        user_states,
        sync_state,
        tmdb_factory: Callable[[AppConfig], TMDbClient],
        imdb_client: IMDbDatasetClient,
        operations: OperationLog,
        episode_metadata: EpisodeMetadataService,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._progress_repo = progress_repo
        self._episode_repo = episode_repo
        self._titles = titles
        self._user_states = user_states
        self._sync_state = sync_state
        self._tmdb_factory = tmdb_factory
        self._imdb_client = imdb_client
        self._operations = operations
        self._policy = SyncPolicy
        self._episode_metadata = episode_metadata

    def refresh_show_progress(self, trakt_id: int, *, fresh: bool = False) -> ProgressSnapshot:
        client = self._auth.get_client()
        progress = client.get_show_progress(trakt_id, use_cache=not fresh)
        title = self._load_or_fetch_show_summary(trakt_id, fallback_title=progress.title)
        progress.title = title.title or progress.title
        progress.poster_url = title.poster_url
        progress.status = title.status
        if progress.next_episode is not None:
            with self._db.session() as session:
                cached_next_episode = self._episode_repo.find_episode(
                    session,
                    trakt_id,
                    progress.next_episode.season,
                    progress.next_episode.number,
                )
            if self._episode_metadata.should_refresh_next_episode_details(progress.next_episode, cached_next_episode):
                detailed_episode = client.get_episode_details(
                    trakt_id,
                    progress.next_episode.season,
                    progress.next_episode.number,
                )
                if detailed_episode is not None:
                    progress.next_episode.trakt_rating = detailed_episode.trakt_rating
                    progress.next_episode.trakt_votes = detailed_episode.trakt_votes
                    if detailed_episode.imdb_id:
                        progress.next_episode.imdb_id = detailed_episode.imdb_id
                    if detailed_episode.runtime is not None:
                        progress.next_episode.runtime = detailed_episode.runtime
                    if detailed_episode.overview:
                        progress.next_episode.overview = detailed_episode.overview
        with self._db.session() as session:
            self._progress_repo.upsert_progress(session, progress)
            if progress.next_episode is not None:
                self._episode_repo.upsert_episode(session, trakt_id, progress.next_episode)
            self._episode_metadata.attach_progress_episode_metadata(session, progress, enrich_imdb=True)
        return progress

    def dashboard_progress(self, *, dropped_only: bool = False) -> list[ProgressSnapshot]:
        with self._db.session() as session:
            items = self._progress_repo.list_in_progress(session, dropped_only=dropped_only)
            for item in items:
                self._episode_metadata.attach_progress_episode_metadata(session, item, enrich_imdb=True)
        for item in items:
            if item.title and not item.title.startswith("Show ") and item.poster_url:
                continue
            summary = self._load_or_fetch_show_summary(item.trakt_id, fallback_title=item.title, persist=False)
            if summary.title and item.title != summary.title:
                item.title = summary.title
            if summary.poster_url and item.poster_url != summary.poster_url:
                item.poster_url = summary.poster_url
            if summary.status and item.status != summary.status:
                item.status = summary.status
        return items

    def sync_progress(self, trakt_ids: list[int] | None = None, *, dropped_only: bool = False) -> list[ProgressSnapshot]:
        if trakt_ids is None and self._can_skip_full_progress_sync():
            self._operations.publish("Progress sync", "Policy skipped full progress sync; using local dashboard state.")
            return self.dashboard_progress(dropped_only=dropped_only)
        self._sync_dropped_status()
        if trakt_ids is None:
            with self._db.session() as session:
                show_ids = self._progress_repo.list_sync_show_ids(session, dropped_only=dropped_only)
            self._operations.publish("Progress sync", f"Full progress refresh for {len(show_ids)} show(s).")
        else:
            show_ids = trakt_ids
            self._operations.publish("Progress sync", f"Focused progress refresh for {len(show_ids)} show(s).")
        snapshots: list[ProgressSnapshot] = []
        for trakt_id in show_ids:
            snapshots.append(self.refresh_show_progress(trakt_id, fresh=True))
        if trakt_ids is None:
            self._remember_progress_activity_signature()
        return snapshots

    def drop_show(self, trakt_id: int) -> None:
        with self._db.session() as session:
            self._user_states.set_archived(session, trakt_id, True)

    def undrop_show(self, trakt_id: int) -> None:
        with self._db.session() as session:
            self._user_states.set_archived(session, trakt_id, False)

    def _sync_dropped_status(self) -> None:
        client = self._auth.get_client()
        dropped_ids: set[int] = set()
        page = 1
        page_size = 100
        while True:
            batch = client.get_dropped_shows(limit=page_size, page=page)
            if not isinstance(batch, list) or not batch:
                break
            for item in batch:
                if not isinstance(item, dict):
                    continue
                show = item.get("show", {}) or {}
                ids = show.get("ids", {}) if isinstance(show, dict) else {}
                trakt_id = ids.get("trakt")
                if trakt_id:
                    dropped_ids.add(int(trakt_id))
            if len(batch) < page_size:
                break
            page += 1
        with self._db.session() as session:
            self._user_states.sync_progress_archived_states(session, dropped_ids)

    def _load_or_fetch_show_summary(self, trakt_id: int, fallback_title: str = "", persist: bool = True) -> TitleSummary:
        with self._db.session() as session:
            stored = self._titles.get_title(session, trakt_id)
            if stored is not None and stored.title and stored.poster_url:
                return TitleSummary(
                    trakt_id=stored.trakt_id,
                    title_type=stored.title_type,
                    title=stored.title,
                    year=stored.year,
                    overview=stored.overview,
                    poster_url=stored.poster_url,
                    status=stored.status,
                    slug=stored.slug,
                )
        client = self._auth.get_client()
        try:
            title = client.get_title_details(trakt_id, "show")
            tmdb = self._tmdb_factory(self._auth.config)
            if tmdb.is_configured():
                title = tmdb.enrich_title(title)
            title = self._imdb_client.enrich_title(title)
        except Exception:
            with self._db.session() as session:
                stored = self._titles.get_title(session, trakt_id)
                if stored is not None:
                    return TitleSummary(
                        trakt_id=stored.trakt_id,
                        title_type=stored.title_type,
                        title=stored.title,
                        year=stored.year,
                        overview=stored.overview,
                        poster_url=stored.poster_url,
                        status=stored.status,
                        slug=stored.slug,
                    )
            return TitleSummary(trakt_id=trakt_id, title_type="show", title=fallback_title or f"Show {trakt_id}")
        if persist:
            with self._db.session() as session:
                self._titles.upsert_title(session, title)
        return title

    def _can_skip_full_progress_sync(self) -> bool:
        with self._db.session() as session:
            has_incomplete_rows = self._progress_repo.has_incomplete_rows(session)
            previous_signature = self._sync_state.get_value(session, SyncPolicy.PROGRESS_SIGNATURE_KEY, "")
            last_full_sync_raw = self._sync_state.get_value(session, SyncPolicy.PROGRESS_LAST_FULL_SYNC_KEY, "")
        current_signature = self._current_progress_activity_signature()
        return self._policy.can_skip_full_progress_sync(
            has_incomplete_rows=has_incomplete_rows,
            current_signature=current_signature,
            previous_signature=previous_signature,
            last_full_sync_raw=last_full_sync_raw,
        )

    def _remember_progress_activity_signature(self) -> None:
        signature = self._current_progress_activity_signature()
        if not signature:
            return
        with self._db.session() as session:
            self._sync_state.set_value(session, SyncPolicy.PROGRESS_SIGNATURE_KEY, signature)
            self._sync_state.set_value(session, SyncPolicy.PROGRESS_LAST_FULL_SYNC_KEY, datetime.now(tz=UTC).isoformat())

    def _current_progress_activity_signature(self) -> str:
        client = self._auth.get_client()
        payload = client.get_last_activities(use_cache=False)
        return self._policy.build_progress_activity_signature(payload)
