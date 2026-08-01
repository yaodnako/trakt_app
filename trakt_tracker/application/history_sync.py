from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.metadata_refresh_policy import ASSET_KIND_POSTER, ASSET_KIND_STILL, TRIGGER_SYNC_EVENT
from trakt_tracker.application.metadata_refresh_policy import TRIGGER_MANUAL_REPAIR
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.application.trakt_payload_cache import (
    load_cached_trakt_history_items,
    load_cached_trakt_rating_items,
)
from trakt_tracker.domain import DashboardState, EpisodeSummary, TitleSummary
from trakt_tracker.infrastructure.artwork_cache import warm_image_urls
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient


WATCH_HISTORY_STREAM_TYPES = (None, "movie")


@dataclass(slots=True)
class _HistoryReconciliationScope:
    title_type: str
    present_history_ids: set[int]
    watched_at_cutoff: datetime | None


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
        catalog=None,
        trakt_outbox=None,
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
        self._catalog = catalog
        self._trakt_outbox = trakt_outbox

    def initial_import(self, *, status_callback=None, defer_enrichment: bool = False) -> None:
        def report(message: str) -> None:
            self._operations.publish("Initial sync", message)
            if status_callback is not None:
                status_callback(message)

        report("Fetching complete Trakt history (5%)")
        self._drain_outbox_before_sync()
        client = self._auth.get_client()
        history_items = self._fetch_all_watch_history(client)
        report("Fetching Trakt ratings (35%)")
        ratings = self._fetch_all_ratings(client)
        self._sync_history_and_ratings(
            history_items,
            ratings,
            reconciliation_scopes=self._complete_reconciliation_scopes(history_items),
            run_enrichment=not defer_enrichment,
        )
        report("Saving history and ratings (85%)")
        signature = self._current_history_activity_signature()
        now = datetime.now(tz=UTC).isoformat()
        with self._db.session() as session:
            if signature:
                self._sync_state.set_value(session, SyncPolicy.HISTORY_SIGNATURE_KEY, signature)
            self._sync_state.set_value(session, SyncPolicy.HISTORY_LAST_SYNC_KEY, now)
            self._sync_state.set_value(session, SyncPolicy.HISTORY_LAST_FULL_RECONCILE_KEY, now)
        report("History import completed (100%)")

    def refresh_history(self, *, force_full_assets: bool = False, status_callback=None) -> None:
        self.sync_updates(force_full_assets=force_full_assets, status_callback=status_callback)

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

    def sync_updates(self, *, force_full_assets: bool = False, status_callback=None) -> None:
        def report(message: str) -> None:
            self._operations.publish("History sync", message)
            if status_callback is not None:
                status_callback(message)

        self._operations.publish("History sync", "Fetching recent history updates and ratings.")
        self._drain_outbox_before_sync()
        client = self._auth.get_client()
        report("Preparing sync (5%)")
        history_items, reconciliation_scopes = self._fetch_recent_history_updates(client)
        with self._db.session() as session:
            last_full_reconcile_at = self._sync_state.get_value(
                session,
                SyncPolicy.HISTORY_LAST_FULL_RECONCILE_KEY,
            )
        full_reconcile = self._policy.should_run_history_full_reconcile(last_full_reconcile_at)
        if full_reconcile:
            full_history_items, reconciliation_scopes = self._fetch_full_history_reconciliation(client)
            with self._db.session() as session:
                known_history_ids = self._history.known_trakt_history_ids(session)
            recovered_items = [
                item
                for item in full_history_items
                if item.get("id") is None or int(item["id"]) not in known_history_ids
            ]
            merged_items: list[dict] = []
            merged_history_ids: set[int] = set()
            self._extend_unique_history_items(merged_items, history_items, merged_history_ids)
            self._extend_unique_history_items(merged_items, recovered_items, merged_history_ids)
            history_items = merged_items
        report("Fetched recent history updates (15%)")
        ratings = self._fetch_all_ratings(client)
        report("Fetched ratings (25%)")
        removed_count = self._sync_history_and_ratings(
            history_items,
            ratings,
            reconciliation_scopes=reconciliation_scopes,
        )
        if full_reconcile:
            with self._db.session() as session:
                self._sync_state.set_value(
                    session,
                    SyncPolicy.HISTORY_LAST_FULL_RECONCILE_KEY,
                    datetime.now(tz=UTC).isoformat(),
                )
        if removed_count:
            report(f"Removed {removed_count} history item(s) no longer present on Trakt")
        report("Merged history and ratings (55%)")
        if force_full_assets:
            self._force_refresh_all_assets(status_callback=status_callback)
            report("Force artwork refresh completed (95%)")
        signature = self._current_history_activity_signature()
        if signature:
            with self._db.session() as session:
                self._sync_state.set_value(session, SyncPolicy.HISTORY_SIGNATURE_KEY, signature)
                self._sync_state.set_value(session, SyncPolicy.HISTORY_LAST_SYNC_KEY, datetime.now(tz=UTC).isoformat())
        report("Finalizing sync (100%)")
        self._operations.publish("History sync", f"Imported {len(history_items)} history item(s) and {len(ratings)} rating item(s).")

    def _drain_outbox_before_sync(self) -> None:
        if self._trakt_outbox is None:
            return
        for _attempt in range(5):
            result = self._trakt_outbox.drain(limit=100)
            if result.processed == 0 or result.delivered == 0:
                break

    def repair_legacy_episode_history(self) -> bool:
        with self._db.session() as session:
            legacy_count = len([row for row in self._history.list_filtered(session, limit=500) if row.title_type == "episode"])
        if legacy_count == 0:
            return False
        profile_slug = self._active_profile_slug()
        history_items = load_cached_trakt_history_items(profile_slug)
        if not history_items:
            client = self._auth.get_client()
            history_items = self._fetch_all_watch_history(client)
        rating_items = load_cached_trakt_rating_items(profile_slug)
        if not rating_items:
            client = self._auth.get_client()
            rating_items = self._fetch_all_ratings(client)
        show_ids: set[int] = set()
        title_sync_targets: set[tuple[int, str]] = set()
        episode_sync_targets: set[tuple[int, int, int]] = set()
        with self._db.session() as session:
            for item in history_items:
                imported = self._import_history_item(session, item)
                if imported is not None and imported["title_type"] == "show":
                    show_ids.add(imported["trakt_id"])
                title_target, episode_target = self._sync_event_targets_from_item(item)
                if title_target is not None:
                    title_sync_targets.add(title_target)
                if episode_target is not None:
                    episode_sync_targets.add(episode_target)
            self._history.clear_ratings(session)
            self._user_states.clear_ratings(session)
            for item in rating_items:
                self._import_rating_item(session, item)
                title_target, episode_target = self._sync_event_targets_from_item(item)
                if title_target is not None:
                    title_sync_targets.add(title_target)
                if episode_target is not None:
                    episode_sync_targets.add(episode_target)
        for trakt_id in show_ids:
            self.refresh_show(trakt_id)
        self._run_sync_event_refreshes(title_sync_targets, episode_sync_targets)
        self._episode_metadata.backfill_episode_imdb_ids_from_payloads(history_items + rating_items)
        self._episode_metadata.repair_episode_imdb_ratings()
        return True

    def refresh_show(self, trakt_id: int, *, fresh: bool = False):
        client = self._auth.get_client()
        progress = client.get_show_progress(trakt_id, use_cache=not fresh)
        if int(progress.completed or 0) <= 0:
            with self._db.session() as session:
                self._progress.delete_progress(session, trakt_id)
            return progress
        episodes = client.get_show_episodes(trakt_id)
        with self._db.session() as session:
            stored = self._titles.get_title(session, trakt_id)
            if not progress.title and stored is not None and stored.title:
                progress.title = stored.title
            self._progress.upsert_progress(session, progress)
            self._episode_repo.replace_show_episodes(session, trakt_id, episodes)
        if self._catalog is not None:
            self._catalog.enrich_title_key(
                trakt_id,
                "show",
                trigger=TRIGGER_SYNC_EVENT,
                requested_parts=(ASSET_KIND_POSTER,),
            )
        self._episode_metadata.repair_episode_imdb_ratings(trakt_id)
        if progress.next_episode is not None:
            self._episode_metadata.enrich_episode_key(
                trakt_id,
                progress.next_episode.season,
                progress.next_episode.number,
                trigger=TRIGGER_SYNC_EVENT,
                requested_parts=(ASSET_KIND_STILL,),
            )
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

    def _sync_history_and_ratings(
        self,
        history_items: list[dict],
        ratings: list[dict],
        *,
        reconciliation_scopes: list[_HistoryReconciliationScope] | None = None,
        run_enrichment: bool = True,
    ) -> int:
        history_intents = (
            self._trakt_outbox.intents(operation_type="history")
            if self._trakt_outbox is not None
            else []
        )
        rating_intents = (
            self._trakt_outbox.intents(operation_type="rating")
            if self._trakt_outbox is not None
            else []
        )
        pending_history_keys = {self._intent_identity(payload) for payload, _desired in history_intents}
        pending_rating_keys = {self._intent_identity(payload) for payload, _desired in rating_intents}
        if pending_history_keys:
            history_items = [item for item in history_items if self._remote_item_identity(item) not in pending_history_keys]
        if pending_rating_keys:
            ratings = [item for item in ratings if self._remote_item_identity(item) not in pending_rating_keys]
        show_ids: set[int] = set()
        title_sync_targets: set[tuple[int, str]] = set()
        episode_sync_targets: set[tuple[int, int, int]] = set()
        removed_title_keys: set[tuple[str, int]] = set()
        removed_show_ids: set[int] = set()
        removed_count = 0
        with self._db.session() as session:
            for scope in reconciliation_scopes or []:
                scope_removed_title_keys, scope_removed_count = self._history.delete_missing_trakt_watches(
                    session,
                    title_type=scope.title_type,
                    present_history_ids=scope.present_history_ids,
                    watched_at_cutoff=scope.watched_at_cutoff,
                )
                removed_title_keys.update(scope_removed_title_keys)
                removed_count += scope_removed_count
            for item in history_items:
                imported = self._import_history_item(session, item)
                if imported is not None and imported["title_type"] == "show":
                    show_ids.add(imported["trakt_id"])
                title_target, episode_target = self._sync_event_targets_from_item(item)
                if title_target is not None:
                    title_sync_targets.add(title_target)
                if episode_target is not None:
                    episode_sync_targets.add(episode_target)
            for payload, desired in history_intents:
                identity = self._intent_identity(payload)
                title_type, trakt_id, season, episode = identity
                self._history.delete_trakt_watches_for_identity(
                    session,
                    title_type=title_type,
                    trakt_id=trakt_id,
                    season=season,
                    episode=episode,
                )
                title_model = self._titles.get_title(session, trakt_id)
                if title_model is not None:
                    state = self._user_states.ensure_state(session, title_model.id)
                    latest = self._history.latest_watch_for_title(
                        session,
                        title_type=title_type,
                        trakt_id=trakt_id,
                    )
                    state.in_history = latest is not None
                    state.last_watched_at = (
                        latest.watched_at
                        if latest is not None and bool(latest.watched_at_known)
                        else None
                    )
                    if title_type == "show" and not bool(desired.get("watched")):
                        removed_show_ids.add(trakt_id)
            for title_type, trakt_id in removed_title_keys:
                title_model = self._titles.get_title(session, trakt_id)
                if title_model is None or str(title_model.title_type) != title_type:
                    continue
                latest = self._history.latest_watch_for_title(
                    session,
                    title_type=title_type,
                    trakt_id=trakt_id,
                )
                state = self._user_states.ensure_state(session, title_model.id)
                state.in_history = latest is not None
                state.last_watched_at = (
                    latest.watched_at
                    if latest is not None and bool(latest.watched_at_known)
                    else None
                )
                if title_type == "show":
                    removed_show_ids.add(int(trakt_id))
                    if latest is None:
                        state.tracked = False
            self._history.clear_ratings(session)
            self._user_states.clear_ratings(session)
            for item in ratings:
                self._import_rating_item(session, item)
                title_target, episode_target = self._sync_event_targets_from_item(item)
                if title_target is not None:
                    title_sync_targets.add(title_target)
                if episode_target is not None:
                    episode_sync_targets.add(episode_target)
            for payload, desired in rating_intents:
                title_type, trakt_id, season, episode = self._intent_identity(payload)
                rating = int(desired.get("rating") or payload.get("rating") or 0)
                if not 1 <= rating <= 10:
                    continue
                model = self._titles.get_title(session, trakt_id)
                if model is None:
                    model = self._titles.upsert_title(
                        session,
                        TitleSummary(
                            trakt_id=trakt_id,
                            title_type=title_type,
                            title=str(payload.get("title") or f"{title_type.capitalize()} {trakt_id}"),
                        ),
                    )
                if season is None and episode is None:
                    self._user_states.ensure_state(session, model.id).rating = rating
                self._history.add_event(
                    session,
                    trakt_history_id=None,
                    title_trakt_id=trakt_id,
                    title=model.title,
                    title_type=title_type,
                    action="rated",
                    watched_at=datetime.now(tz=UTC),
                    season=season,
                    episode=episode,
                    rating=rating,
                    source="local",
                )
                self._history.apply_rating_to_latest_watch(
                    session,
                    title_trakt_id=trakt_id,
                    title_type=title_type,
                    season=season,
                    episode=episode,
                    rating=rating,
                )
            self._sync_state.set_value(session, "initial_import_at", datetime.now(tz=UTC).isoformat())
        for trakt_id in sorted(show_ids | removed_show_ids):
            self.refresh_show(trakt_id, fresh=trakt_id in removed_show_ids)
        if run_enrichment:
            self._run_sync_event_refreshes(title_sync_targets, episode_sync_targets)
            self._episode_metadata.backfill_episode_imdb_ids_from_payloads(history_items + ratings)
            self._episode_metadata.repair_episode_imdb_ratings()
        return removed_count

    @staticmethod
    def _intent_identity(payload: dict) -> tuple[str, int, int | None, int | None]:
        return (
            "show" if payload.get("title_type") == "show" else "movie",
            int(payload.get("trakt_id") or 0),
            int(payload["season"]) if payload.get("season") is not None else None,
            int(payload["episode"]) if payload.get("episode") is not None else None,
        )

    @staticmethod
    def _remote_item_identity(item: dict) -> tuple[str, int, int | None, int | None]:
        raw_type = str(item.get("type") or "")
        if raw_type == "episode":
            show = item.get("show", {}) if isinstance(item.get("show"), dict) else {}
            episode = item.get("episode", {}) if isinstance(item.get("episode"), dict) else {}
            ids = show.get("ids", {}) if isinstance(show.get("ids"), dict) else {}
            return (
                "show",
                int(ids.get("trakt") or 0),
                int(episode["season"]) if episode.get("season") is not None else None,
                int(episode["number"]) if episode.get("number") is not None else None,
            )
        entity = item.get(raw_type, {}) if isinstance(item.get(raw_type), dict) else {}
        ids = entity.get("ids", {}) if isinstance(entity.get("ids"), dict) else {}
        return ("show" if raw_type == "show" else "movie", int(ids.get("trakt") or 0), None, None)

    @staticmethod
    def _sync_event_targets_from_item(item: dict) -> tuple[tuple[int, str] | None, tuple[int, int, int] | None]:
        raw_type = str(item.get("type", "") or "")
        if raw_type == "episode":
            show_payload = item.get("show", {}) or {}
            episode_payload = item.get("episode", {}) or {}
            show_ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
            show_trakt_id = show_ids.get("trakt")
            season = episode_payload.get("season")
            number = episode_payload.get("number")
            title_target = (int(show_trakt_id), "show") if show_trakt_id else None
            episode_target = (
                (int(show_trakt_id), int(season), int(number))
                if show_trakt_id and season is not None and number is not None
                else None
            )
            return title_target, episode_target
        payload = item.get(raw_type, {}) or {}
        ids = payload.get("ids", {}) if isinstance(payload, dict) else {}
        trakt_id = ids.get("trakt")
        if trakt_id and raw_type in {"movie", "show"}:
            return (int(trakt_id), raw_type), None
        return None, None

    def _run_sync_event_refreshes(
        self,
        title_targets: set[tuple[int, str]],
        episode_targets: set[tuple[int, int, int]],
    ) -> None:
        if self._catalog is not None:
            for trakt_id, title_type in sorted(title_targets):
                self._catalog.enrich_title_key(
                    trakt_id,
                    title_type,
                    trigger=TRIGGER_SYNC_EVENT,
                    requested_parts=(ASSET_KIND_POSTER,),
                )
        for show_trakt_id, season, episode in sorted(episode_targets):
            self._episode_metadata.enrich_episode_key(
                show_trakt_id,
                season,
                episode,
                trigger=TRIGGER_SYNC_EVENT,
                requested_parts=(ASSET_KIND_STILL,),
            )

    def _force_refresh_all_assets(self, *, status_callback=None) -> None:
        with self._db.session() as session:
            all_titles = self._titles.list_titles(session)
            all_show_ids = self._episode_repo.list_cached_show_ids(session)
        self._operations.publish(
            "History sync",
            f"Force refreshing artwork for {len(all_titles)} title(s) and {len(all_show_ids)} show cache(s).",
        )
        total_steps = len(all_titles) + len(all_show_ids)
        completed_steps = 0
        last_percent = -1.0

        def emit(message: str) -> None:
            self._operations.publish("History sync", message)
            if status_callback is not None:
                status_callback(message)

        def report_progress() -> None:
            nonlocal last_percent
            if total_steps <= 0:
                return
            percent = round((completed_steps * 100.0) / total_steps, 1)
            if percent == last_percent:
                return
            last_percent = percent
            emit(f"Artwork refresh {completed_steps}/{total_steps} ({percent:.1f}%)")

        if self._catalog is not None:
            image_cache = BinaryCache("images")
            for title in all_titles:
                try:
                    self._catalog.enrich_title_key(
                        int(title.trakt_id),
                        str(title.title_type),
                        trigger=TRIGGER_MANUAL_REPAIR,
                        requested_parts=(ASSET_KIND_POSTER,),
                    )
                    with self._db.session() as session:
                        refreshed = self._titles.get_title(session, int(title.trakt_id))
                        poster_url = str(refreshed.poster_url or "") if refreshed is not None else ""
                    if poster_url:
                        warm_image_urls(image_cache, [poster_url], timeout=8, max_workers=1)
                except Exception as exc:
                    emit(f"Poster refresh failed for {title.title_type}:{title.trakt_id}: {exc}")
                completed_steps += 1
                report_progress()
        image_cache = BinaryCache("images")
        for show_trakt_id in all_show_ids:
            try:
                self._episode_metadata.force_refresh_show_stills(int(show_trakt_id))
                with self._db.session() as session:
                    urls = [
                        str(row.get("still_url") or "")
                        for row in self._episode_repo.list_show_episode_metadata(session, int(show_trakt_id))
                        if row.get("still_url")
                    ]
                warm_image_urls(image_cache, urls, timeout=8, max_workers=4)
            except Exception as exc:
                emit(f"Still refresh failed for show:{show_trakt_id}: {exc}")
            completed_steps += 1
            report_progress()

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
                trakt_rating=self._as_float(show_payload.get("rating")),
                trakt_votes=self._as_int(show_payload.get("votes")),
                tmdb_id=self._as_int(ids.get("tmdb")),
                imdb_id=str(ids.get("imdb", "") or ""),
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
                        trakt_rating=self._as_float(episode_payload.get("rating")),
                        trakt_votes=self._as_int(episode_payload.get("votes")),
                        imdb_id=str(episode_ids.get("imdb", "") or ""),
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
                trakt_rating=self._as_float(payload.get("rating")),
                trakt_votes=self._as_int(payload.get("votes")),
                tmdb_id=self._as_int(ids.get("tmdb")),
                imdb_id=str(ids.get("imdb", "") or ""),
            )
        model = self._titles.upsert_title(session, title)
        state = self._user_states.ensure_state(session, model.id)
        state.in_history = True
        state.tracked = title_type == "show"
        watched_at_raw = item.get("watched_at")
        watched_at_known = bool(watched_at_raw)
        watched_at = datetime.fromisoformat(watched_at_raw.replace("Z", "+00:00")) if watched_at_raw else datetime(1970, 1, 1, tzinfo=UTC)
        if watched_at_known:
            current_last_watched_at = state.last_watched_at
            current_utc = (
                current_last_watched_at.replace(tzinfo=UTC)
                if current_last_watched_at is not None and current_last_watched_at.tzinfo is None
                else current_last_watched_at.astimezone(UTC)
                if current_last_watched_at is not None
                else None
            )
            watched_at_utc = watched_at.replace(tzinfo=UTC) if watched_at.tzinfo is None else watched_at.astimezone(UTC)
            if current_utc is None or watched_at_utc > current_utc:
                state.last_watched_at = watched_at
        self._history.add_event(
            session,
            trakt_history_id=item.get("id"),
            title_trakt_id=trakt_id,
            title=title.title,
            title_type=title_type,
            action="watched",
            watched_at=watched_at,
            watched_at_known=watched_at_known,
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
                trakt_rating=self._as_float(show_payload.get("rating")),
                trakt_votes=self._as_int(show_payload.get("votes")),
                tmdb_id=self._as_int(ids.get("tmdb")),
                imdb_id=str(ids.get("imdb", "") or ""),
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
                        trakt_rating=self._as_float(episode_payload.get("rating")),
                        trakt_votes=self._as_int(episode_payload.get("votes")),
                        imdb_id=str(episode_ids.get("imdb", "") or ""),
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
            trakt_rating=self._as_float(payload.get("rating")),
            trakt_votes=self._as_int(payload.get("votes")),
            tmdb_id=self._as_int(ids.get("tmdb")),
            imdb_id=str(ids.get("imdb", "") or ""),
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
        seen_history_ids: set[int] = set()
        for title_type in WATCH_HISTORY_STREAM_TYPES:
            page = 1
            while True:
                batch = HistorySyncWorkflow._authoritative_page(
                    client.get_watch_history,
                    title_type=title_type,
                    limit=page_size,
                    page=page,
                )
                if not batch:
                    break
                HistorySyncWorkflow._extend_unique_history_items(items, batch, seen_history_ids)
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
                batch = HistorySyncWorkflow._authoritative_page(
                    client.get_ratings,
                    title_type=title_type,
                    limit=page_size,
                    page=page,
                )
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

    def _fetch_recent_history_updates(
        self,
        client,
        page_size: int = 100,
    ) -> tuple[list[dict], list[_HistoryReconciliationScope]]:
        with self._db.session() as session:
            known_ids = self._history.known_trakt_history_ids(session)
        if not known_ids:
            items = self._fetch_all_watch_history(client, page_size=page_size)
            return items, self._complete_reconciliation_scopes(items)
        items: list[dict] = []
        seen_history_ids: set[int] = set()
        reconciliation_scopes: list[_HistoryReconciliationScope] = []
        for title_type in WATCH_HISTORY_STREAM_TYPES:
            scope_title_type = "show" if title_type is None else "movie"
            scanned_scope_items: list[dict] = []
            complete = False
            page = 1
            while True:
                batch = self._authoritative_page(
                    client.get_watch_history,
                    title_type=title_type,
                    limit=page_size,
                    page=page,
                )
                if not batch:
                    complete = True
                    break
                scanned_scope_items.extend(
                    item for item in batch if self._history_item_title_type(item) == scope_title_type
                )
                unseen = [item for item in batch if item.get("id") not in known_ids]
                if unseen:
                    self._extend_unique_history_items(items, unseen, seen_history_ids)
                if len(batch) < page_size:
                    complete = True
                    break
                if not unseen:
                    break
                page += 1
            scope = self._reconciliation_scope(
                scope_title_type,
                scanned_scope_items,
                complete=complete,
            )
            if scope is not None:
                reconciliation_scopes.append(scope)
        return items, reconciliation_scopes

    @classmethod
    def _fetch_full_history_reconciliation(
        cls,
        client,
        page_size: int = 100,
    ) -> tuple[list[dict], list[_HistoryReconciliationScope]]:
        items: list[dict] = []
        seen_history_ids: set[int] = set()
        scopes: list[_HistoryReconciliationScope] = []
        load_page = getattr(client, "get_watch_history_page", None)
        for title_type in WATCH_HISTORY_STREAM_TYPES:
            scope_title_type = "show" if title_type is None else "movie"
            scanned_scope_items: list[dict] = []
            page = 1
            while True:
                page_count = None
                if callable(load_page):
                    batch, headers = load_page(title_type=title_type, limit=1000, page=page)
                    try:
                        page_count = int(
                            headers.get("x-pagination-page-count")
                            or headers.get("X-Pagination-Page-Count")
                            or 0
                        )
                    except (TypeError, ValueError):
                        page_count = None
                else:
                    batch = cls._authoritative_page(
                        client.get_watch_history,
                        title_type=title_type,
                        limit=page_size,
                        page=page,
                    )
                cls._extend_unique_history_items(items, batch, seen_history_ids)
                scanned_scope_items.extend(
                    item for item in batch if cls._history_item_title_type(item) == scope_title_type
                )
                if page_count is not None and page_count > 0:
                    if page >= page_count:
                        break
                elif not batch or (not callable(load_page) and len(batch) < page_size):
                    break
                page += 1
            scope = cls._reconciliation_scope(scope_title_type, scanned_scope_items, complete=True)
            assert scope is not None
            scopes.append(scope)
        return items, scopes

    @classmethod
    def _complete_reconciliation_scopes(cls, items: list[dict]) -> list[_HistoryReconciliationScope]:
        return [
            scope
            for title_type in ("show", "movie")
            if (
                scope := cls._reconciliation_scope(
                    title_type,
                    [item for item in items if cls._history_item_title_type(item) == title_type],
                    complete=True,
                )
            ) is not None
        ]

    @staticmethod
    def _authoritative_page(fetch_page, **kwargs):
        try:
            return fetch_page(**kwargs, authoritative=True)
        except TypeError as exc:
            if "authoritative" not in str(exc):
                raise
            return fetch_page(**kwargs)

    @classmethod
    def _reconciliation_scope(
        cls,
        title_type: str,
        items: list[dict],
        *,
        complete: bool,
    ) -> _HistoryReconciliationScope | None:
        timestamps = [
            watched_at
            for item in items
            if (watched_at := cls._parse_history_watched_at(item.get("watched_at"))) is not None
        ]
        if not complete and not timestamps:
            return None
        return _HistoryReconciliationScope(
            title_type=title_type,
            present_history_ids={
                int(item["id"])
                for item in items
                if item.get("id") is not None
            },
            watched_at_cutoff=None if complete else min(timestamps),
        )

    @staticmethod
    def _history_item_title_type(item: dict) -> str:
        raw_type = str(item.get("type", "") or "")
        if raw_type == "movie":
            return "movie"
        if raw_type in {"episode", "show"}:
            return "show"
        return ""

    @staticmethod
    def _parse_history_watched_at(value) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _extend_unique_history_items(target: list[dict], batch: list[dict], seen_history_ids: set[int]) -> None:
        for item in batch:
            history_id = item.get("id")
            if history_id is not None:
                history_id = int(history_id)
                if history_id in seen_history_ids:
                    continue
                seen_history_ids.add(history_id)
            target.append(item)

    def _current_history_activity_signature(self) -> str:
        client = self._auth.get_client()
        payload = client.get_last_activities(use_cache=False)
        return self._policy.build_history_activity_signature(payload)

    def _active_profile_slug(self) -> str:
        config = getattr(self._auth, "config", None)
        return str(getattr(config, "active_slug", "") or "")

    @staticmethod
    def _as_float(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
