from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.application.enrich_queue import TASK_RESULT_SKIPPED_ALREADY_RESOLVED
from trakt_tracker.application.episode_imdb_resolver import EpisodeIMDbResolver
from trakt_tracker.application.episode_imdb_reconciliation import (
    IMDB_MATCH_STATUS_RESOLVED,
    EpisodeIMDbReconciliationService,
)
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_STILL,
    EPISODE_ALLOWED_PARTS,
    TRIGGER_MANUAL_REPAIR,
    TRIGGER_SYNC_EVENT,
    TRIGGER_VIEWPORT,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
    MetadataRefreshRequest,
    build_refresh_request,
    metadata_refresh_due,
    refresh_requests_from_payload,
)
from trakt_tracker.application.trakt_payload_cache import (
    load_cached_trakt_history_items,
    load_cached_trakt_rating_items,
)
from trakt_tracker.config import AppConfig
from trakt_tracker.domain import EpisodeSummary
from trakt_tracker.infrastructure.tmdb import TMDbClient


class EpisodeMetadataService:
    def __init__(
        self,
        db,
        episode_repo,
        imdb_client,
        titles_repo=None,
        auth_service=None,
        tmdb_factory: Callable[[AppConfig], TMDbClient] | None = None,
        imdb_reconciliation=None,
    ) -> None:
        self._db = db
        self._episode_repo = episode_repo
        self._imdb_client = imdb_client
        self._titles = titles_repo
        self._auth = auth_service
        self._tmdb_factory = tmdb_factory
        self._imdb_resolver = EpisodeIMDbResolver(imdb_client)
        self._imdb_reconciliation = imdb_reconciliation or EpisodeIMDbReconciliationService(db, episode_repo, imdb_client)

    def _normalize_episode_refresh_requests(
        self,
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> tuple[MetadataRefreshRequest, ...]:
        if refresh_requests is not None:
            normalized = refresh_requests_from_payload(refresh_requests, allowed_parts=EPISODE_ALLOWED_PARTS)
            if normalized:
                return normalized
        return (
            build_refresh_request(
                trigger=trigger,
                requested_parts=requested_parts,
                allowed_parts=EPISODE_ALLOWED_PARTS,
            ),
        )

    @staticmethod
    def _episode_record_value(record, dict_key: str, attr_name: str):
        if record is None:
            return None
        if isinstance(record, dict):
            return record.get(dict_key)
        return getattr(record, attr_name, None)

    def _episode_refresh_parts(
        self,
        record,
        refresh_requests: tuple[MetadataRefreshRequest, ...],
    ) -> dict[str, str]:
        parts: dict[str, str] = {}
        still_url = str(self._episode_record_value(record, "still_url", "still_url") or "")
        still_status = str(self._episode_record_value(record, "still_status", "still_status") or ENRICH_STATUS_UNKNOWN)
        still_refreshed_at = self._episode_record_value(record, "still_refreshed_at", "still_refreshed_at")
        first_aired = self._episode_record_value(record, "first_aired", "first_aired")
        trakt_details_status = str(
            self._episode_record_value(record, "trakt_details_status", "trakt_details_status") or ENRICH_STATUS_UNKNOWN
        )
        trakt_details_refreshed_at = self._episode_record_value(
            record,
            "trakt_details_refreshed_at",
            "trakt_details_refreshed_at",
        )
        has_ratings_value = any(
            self._episode_record_value(record, dict_key, attr_name) is not None
            for dict_key, attr_name in (
                ("trakt_rating", "trakt_rating"),
                ("imdb_rating", "imdb_rating"),
            )
        )
        for request in refresh_requests:
            requested = request.requested_parts or EPISODE_ALLOWED_PARTS
            if ASSET_KIND_EPISODE_RATINGS in requested:
                ratings_decision = metadata_refresh_due(
                    ASSET_KIND_EPISODE_RATINGS,
                    status=trakt_details_status,
                    last_checked_at=trakt_details_refreshed_at,
                    has_value=has_ratings_value,
                    trigger=request.trigger,
                    first_aired=first_aired,
                )
                if ratings_decision.should_refresh:
                    parts[ASSET_KIND_EPISODE_RATINGS] = ratings_decision.reason
            if ASSET_KIND_STILL in requested and self.can_enrich_episode_stills():
                still_decision = metadata_refresh_due(
                    ASSET_KIND_STILL,
                    status=still_status,
                    last_checked_at=still_refreshed_at,
                    has_value=bool(still_url),
                    trigger=request.trigger,
                    first_aired=first_aired,
                )
                if still_decision.should_refresh:
                    parts[ASSET_KIND_STILL] = still_decision.reason
        return parts

    def load_cached_trakt_rating_maps(self) -> tuple[dict[int, int], dict[tuple[int, int, int], int]]:
        title_ratings: dict[int, tuple[datetime, int]] = {}
        episode_ratings: dict[tuple[int, int, int], tuple[datetime, int]] = {}
        for item in load_cached_trakt_rating_items(self._active_profile_slug()):
            if not isinstance(item, dict):
                continue
            rating = item.get("rating")
            if not isinstance(rating, int):
                continue
            rated_at_raw = item.get("rated_at")
            try:
                rated_at = datetime.fromisoformat(str(rated_at_raw).replace("Z", "+00:00")) if rated_at_raw else datetime.min.replace(tzinfo=UTC)
            except ValueError:
                rated_at = datetime.min.replace(tzinfo=UTC)
            raw_type = item.get("type")
            if raw_type == "episode":
                show_payload = item.get("show", {}) or {}
                episode_payload = item.get("episode", {}) or {}
                show_ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
                show_trakt_id = show_ids.get("trakt")
                season = episode_payload.get("season")
                number = episode_payload.get("number")
                if not show_trakt_id or season is None or number is None:
                    continue
                key = (int(show_trakt_id), int(season), int(number))
                existing = episode_ratings.get(key)
                if existing is None or rated_at >= existing[0]:
                    episode_ratings[key] = (rated_at, rating)
                continue
            payload = item.get(raw_type, {}) or {}
            ids = payload.get("ids", {}) if isinstance(payload, dict) else {}
            trakt_id = ids.get("trakt")
            if not trakt_id:
                continue
            trakt_id = int(trakt_id)
            existing = title_ratings.get(trakt_id)
            if existing is None or rated_at >= existing[0]:
                title_ratings[trakt_id] = (rated_at, rating)
        return (
            {trakt_id: rating for trakt_id, (_rated_at, rating) in title_ratings.items()},
            {key: rating for key, (_rated_at, rating) in episode_ratings.items()},
        )

    def load_cached_episode_imdb_metadata(self, keys: list[tuple[int, int, int]]) -> dict[tuple[int, int, int], dict]:
        if not keys or not self._imdb_client.is_ready():
            return {}
        wanted = set(keys)
        result: dict[tuple[int, int, int], dict] = {}
        profile_slug = self._active_profile_slug()
        payloads = (
            load_cached_trakt_history_items(profile_slug)
            + load_cached_trakt_rating_items(profile_slug)
        )
        for item in payloads:
            if item.get("type") != "episode":
                continue
            show_payload = item.get("show", {}) or {}
            episode_payload = item.get("episode", {}) or {}
            show_ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
            episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
            key = (show_ids.get("trakt"), episode_payload.get("season"), episode_payload.get("number"))
            if key not in wanted:
                continue
            show_imdb_id = str(show_ids.get("imdb", "") or "")
            season_number = episode_payload.get("season")
            episode_number = episode_payload.get("number")
            if not show_imdb_id or season_number is None or episode_number is None:
                continue
            resolution = self._imdb_resolver.resolve(
                show_imdb_id=show_imdb_id,
                season=int(season_number),
                episode=int(episode_number),
                title=str(episode_payload.get("title", "") or ""),
                trakt_imdb_id=str(episode_ids.get("imdb", "") or ""),
            )
            if not resolution.imdb_id:
                continue
            result[key] = {
                "imdb_id": resolution.imdb_id,
                "imdb_rating": resolution.imdb_rating,
                "imdb_votes": resolution.imdb_votes,
            }
        return result

    def _active_profile_slug(self) -> str:
        config = getattr(self._auth, "config", None)
        return str(getattr(config, "active_slug", "") or "")

    def enrich_episode_imdb_ratings(self) -> None:
        if not self._imdb_client.is_ready():
            return
        with self._db.session() as session:
            rows = self._episode_repo.list_all_with_imdb(session)
            for row in rows:
                show_imdb_id = self._show_imdb_id(session, int(row.show_trakt_id))
                if not row.imdb_id and not show_imdb_id:
                    continue
                resolution = self._imdb_resolver.resolve(
                    show_imdb_id=show_imdb_id,
                    season=int(row.season),
                    episode=int(row.number),
                    title=row.title,
                    trakt_imdb_id=row.imdb_id,
                )
                row.imdb_id = resolution.imdb_id
                row.imdb_rating = resolution.imdb_rating
                row.imdb_votes = resolution.imdb_votes
                row.imdb_season = resolution.imdb_season
                row.imdb_episode = resolution.imdb_episode
                if resolution.imdb_id:
                    row.imdb_match_status = IMDB_MATCH_STATUS_RESOLVED
                    row.imdb_match_attempt_key = ""

    def repair_episode_imdb_ratings(self, show_trakt_id: int | None = None) -> int:
        if not self._imdb_client.is_ready() or self._titles is None:
            return 0
        with self._db.session() as session:
            show_ids = [int(show_trakt_id)] if show_trakt_id is not None else self._episode_repo.list_cached_show_ids(session)
        changed = 0
        for current_show_id in show_ids:
            with self._db.session() as session:
                show_imdb_id = self._show_imdb_id(session, current_show_id)
                episode_rows = self._episode_repo.list_show_episode_metadata(session, current_show_id)
            if not self._imdb_reconciliation.needs_reconciliation(
                show_imdb_id=show_imdb_id,
                episode_rows=episode_rows,
            ):
                continue
            result = self._imdb_reconciliation.reconcile_show(current_show_id, show_imdb_id=show_imdb_id)
            changed += result.changed
        return changed

    def needs_episode_imdb_reconciliation(self, show_trakt_id: int) -> bool:
        if not self._imdb_client.is_ready() or self._titles is None:
            return False
        with self._db.session() as session:
            show_imdb_id = self._show_imdb_id(session, int(show_trakt_id))
            episode_rows = self._episode_repo.list_show_episode_metadata(session, int(show_trakt_id))
        return self._imdb_reconciliation.needs_reconciliation(
            show_imdb_id=show_imdb_id,
            episode_rows=episode_rows,
        )

    def backfill_episode_imdb_ids_from_payloads(self, payloads: list[dict]) -> None:
        if not payloads:
            return
        with self._db.session() as session:
            for item in payloads:
                if item.get("type") != "episode":
                    continue
                show_payload = item.get("show", {}) or {}
                episode_payload = item.get("episode", {}) or {}
                show_ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
                episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
                show_trakt_id = show_ids.get("trakt")
                season = episode_payload.get("season")
                number = episode_payload.get("number")
                if not show_trakt_id or season is None or number is None:
                    continue
                row = self._episode_repo.find_episode(session, show_trakt_id, season, number)
                if row is None:
                    continue
                show_imdb_id = str(show_ids.get("imdb", "") or "")
                if not show_imdb_id:
                    continue
                resolution = self._imdb_resolver.resolve(
                    show_imdb_id=show_imdb_id,
                    season=int(season),
                    episode=int(number),
                    title=str(episode_payload.get("title", "") or ""),
                    trakt_imdb_id=str(episode_ids.get("imdb", "") or ""),
                )
                if resolution.imdb_id and not row.imdb_id:
                    row.imdb_id = resolution.imdb_id
                    row.imdb_rating = resolution.imdb_rating
                    row.imdb_votes = resolution.imdb_votes
                    row.imdb_season = resolution.imdb_season
                    row.imdb_episode = resolution.imdb_episode
                    row.imdb_match_status = IMDB_MATCH_STATUS_RESOLVED
                    row.imdb_match_attempt_key = ""

    def attach_progress_episode_metadata(self, session, progress, *, enrich_imdb: bool = False) -> None:
        if progress.next_episode is None:
            return
        row = self._episode_repo.find_episode(
            session,
            progress.trakt_id,
            progress.next_episode.season,
            progress.next_episode.number,
        )
        if row is None:
            return
        progress.next_episode.trakt_rating = row.trakt_rating
        progress.next_episode.trakt_votes = row.trakt_votes
        progress.next_episode.imdb_id = row.imdb_id
        if enrich_imdb and row.imdb_id and (row.imdb_rating is None or row.imdb_votes is None):
            resolution = self._imdb_resolver.resolve(
                show_imdb_id=self._show_imdb_id(session, int(row.show_trakt_id)),
                season=int(row.season),
                episode=int(row.number),
                title=row.title,
                trakt_imdb_id=row.imdb_id,
            )
            row.imdb_id = resolution.imdb_id
            row.imdb_rating = resolution.imdb_rating
            row.imdb_votes = resolution.imdb_votes
            row.imdb_season = resolution.imdb_season
            row.imdb_episode = resolution.imdb_episode
        progress.next_episode.still_url = row.still_url or ""
        progress.next_episode.still_status = row.still_status or ENRICH_STATUS_UNKNOWN
        progress.next_episode.still_refreshed_at = row.still_refreshed_at
        progress.next_episode.trakt_details_status = row.trakt_details_status or ENRICH_STATUS_UNKNOWN
        progress.next_episode.trakt_details_refreshed_at = row.trakt_details_refreshed_at
        progress.next_episode.imdb_rating = row.imdb_rating
        progress.next_episode.imdb_votes = row.imdb_votes
        progress.next_episode.imdb_season = row.imdb_season
        progress.next_episode.imdb_episode = row.imdb_episode
        progress.next_episode.imdb_status = (
            ENRICH_STATUS_READY
            if row.imdb_rating is not None and row.imdb_votes is not None
            else (ENRICH_STATUS_CHECKED_NO_DATA if row.imdb_id else ENRICH_STATUS_UNKNOWN)
        )

    def select_episode_enrich_keys(
        self,
        rows: list[dict],
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> list[tuple[int, int, int]]:
        episode_keys = [
            (int(row["title_trakt_id"]), int(row["season"]), int(row["episode"]))
            for row in rows
            if row.get("type") == "show" and row.get("season") is not None and row.get("episode") is not None
        ]
        if not episode_keys:
            return []
        with self._db.session() as session:
            metadata = self._episode_repo.metadata_by_episode_keys(session, episode_keys)
        normalized_requests = self._normalize_episode_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        pending: list[tuple[int, int, int]] = []
        for key in dict.fromkeys(episode_keys):
            item = metadata.get(key) or {}
            if self._episode_refresh_parts(item, normalized_requests):
                pending.append(key)
        return pending

    def episode_key_needs_enrich(
        self,
        show_trakt_id: int,
        season: int,
        episode: int,
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> bool:
        with self._db.session() as session:
            item = self._episode_repo.metadata_by_episode_keys(session, [(show_trakt_id, season, episode)]).get(
                (show_trakt_id, season, episode),
                {},
            )
        normalized_requests = self._normalize_episode_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        return bool(self._episode_refresh_parts(item, normalized_requests))

    def enrich_episode_key(
        self,
        show_trakt_id: int,
        season: int,
        episode: int,
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(),
        refresh_requests=None,
    ) -> str:
        normalized_requests = self._normalize_episode_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        if not self.episode_key_needs_enrich(
            show_trakt_id,
            season,
            episode,
            refresh_requests=tuple(request.to_payload() for request in normalized_requests),
        ):
            return TASK_RESULT_SKIPPED_ALREADY_RESOLVED

        with self._db.session() as session:
            item = self._episode_repo.metadata_by_episode_keys(session, [(show_trakt_id, season, episode)]).get(
                (show_trakt_id, season, episode),
                {},
            )
        due_parts = self._episode_refresh_parts(item, normalized_requests)
        needs_details = ASSET_KIND_EPISODE_RATINGS in due_parts
        needs_still = ASSET_KIND_STILL in due_parts
        force_network = needs_details or any(
            request.trigger in {TRIGGER_VISIBLE_RATINGS_REFRESH, TRIGGER_SYNC_EVENT, TRIGGER_MANUAL_REPAIR}
            for request in normalized_requests
        )
        if needs_details and self._auth is not None:
            client = self._auth.get_client()
            try:
                details = client.get_episode_details(show_trakt_id, season, episode, use_cache=not force_network)
            except Exception:
                with self._db.session() as session:
                    self._episode_repo.update_trakt_details_enrich_state(
                        session,
                        show_trakt_id,
                        season,
                        episode,
                        status=ENRICH_STATUS_RETRYABLE_FAILURE,
                    )
            else:
                if details is None:
                    with self._db.session() as session:
                        self._episode_repo.update_trakt_details_enrich_state(
                            session,
                            show_trakt_id,
                            season,
                            episode,
                            status=ENRICH_STATUS_CHECKED_NO_DATA,
                        )
                else:
                    with self._db.session() as session:
                        show_imdb_id = self._show_imdb_id(session, show_trakt_id)
                    self._resolve_episode_summary_for_show(show_imdb_id, details)
                    status = (
                        ENRICH_STATUS_CHECKED_NO_DATA
                        if details.trakt_rating is None or details.trakt_votes is None
                        else ENRICH_STATUS_READY
                    )
                    with self._db.session() as session:
                        self._episode_repo.update_trakt_details_enrich_state(
                            session,
                            show_trakt_id,
                            season,
                            episode,
                            status=status,
                            details=details,
                        )
        if needs_still:
            try:
                self.enrich_episode_stills(
                    [(show_trakt_id, season, episode)],
                    refresh_requests=tuple(request.to_payload() for request in normalized_requests),
                )
            except Exception:
                pass

        with self._db.session() as session:
            item = self._episode_repo.metadata_by_episode_keys(session, [(show_trakt_id, season, episode)]).get(
                (show_trakt_id, season, episode),
                {},
            )
        if self._episode_refresh_parts(item, normalized_requests):
            return ENRICH_STATUS_RETRYABLE_FAILURE
        if (
            (needs_still and item.get("still_url"))
            or (needs_details and (item.get("trakt_rating") is not None or item.get("imdb_rating") is not None))
            or (not needs_still and not needs_details and (item.get("still_url") or item.get("trakt_rating") is not None or item.get("imdb_rating") is not None))
        ):
            return ENRICH_STATUS_READY
        return ENRICH_STATUS_CHECKED_NO_DATA

    def _show_imdb_id(self, session, show_trakt_id: int) -> str:
        if self._titles is None:
            return ""
        title = self._titles.get_title(session, show_trakt_id)
        return str(title.imdb_id or "") if title is not None else ""

    def _resolve_episode_summary(self, session, *, show_trakt_id: int, episode: EpisodeSummary) -> None:
        show_imdb_id = self._show_imdb_id(session, show_trakt_id)
        self._resolve_episode_summary_for_show(show_imdb_id, episode)

    def _resolve_episode_summary_for_show(self, show_imdb_id: str, episode: EpisodeSummary) -> None:
        if not show_imdb_id:
            return
        resolution = self._imdb_resolver.resolve(
            show_imdb_id=show_imdb_id,
            season=episode.season,
            episode=episode.number,
            title=episode.title,
            trakt_imdb_id=episode.imdb_id,
        )
        episode.imdb_id = resolution.imdb_id
        episode.imdb_rating = resolution.imdb_rating
        episode.imdb_votes = resolution.imdb_votes

    def can_enrich_episode_stills(self) -> bool:
        if self._auth is None or self._tmdb_factory is None:
            return False
        return self._tmdb_factory(self._auth.config).is_configured()

    def enrich_episode_stills(
        self,
        keys: list[tuple[int, int, int]],
        *,
        trigger: str = TRIGGER_VIEWPORT,
        requested_parts=(ASSET_KIND_STILL,),
        refresh_requests=None,
    ) -> bool:
        if not keys or not self.can_enrich_episode_stills():
            return False
        unique_keys = list(dict.fromkeys(keys))
        normalized_requests = self._normalize_episode_refresh_requests(
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )
        with self._db.session() as session:
            metadata = self._episode_repo.metadata_by_episode_keys(session, unique_keys)
        missing_by_show: dict[int, list[tuple[int, int]]] = {}
        for show_trakt_id, season, episode in unique_keys:
            item = metadata.get((show_trakt_id, season, episode)) or {}
            if ASSET_KIND_STILL not in self._episode_refresh_parts(item, normalized_requests):
                continue
            missing_by_show.setdefault(show_trakt_id, []).append((season, episode))
        if not missing_by_show:
            return False
        client = self._auth.get_client()
        tmdb = self._tmdb_factory(self._auth.config)
        changed = False
        for show_trakt_id, episodes in missing_by_show.items():
            try:
                show_tmdb_id = self._load_show_tmdb_id(show_trakt_id, client)
            except Exception:
                self._set_still_states(show_trakt_id, episodes, status=ENRICH_STATUS_RETRYABLE_FAILURE)
                continue
            if not show_tmdb_id:
                self._set_still_states(show_trakt_id, episodes, status=ENRICH_STATUS_CHECKED_NO_DATA)
                continue
            episodes_by_season: dict[int, list[int]] = {}
            for season, episode in episodes:
                episodes_by_season.setdefault(season, []).append(episode)
            for season, episode_numbers in episodes_by_season.items():
                try:
                    season_stills = self._season_still_urls(tmdb, show_tmdb_id, season, episode_numbers)
                except Exception:
                    self._set_still_states(
                        show_trakt_id,
                        [(season, episode) for episode in episode_numbers],
                        status=ENRICH_STATUS_RETRYABLE_FAILURE,
                    )
                    continue
                with self._db.session() as session:
                    for episode in episode_numbers:
                        still_url = season_stills.get(episode, "")
                        row = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
                        if row is None:
                            continue
                        if still_url:
                            if row.still_url != still_url or row.still_status != ENRICH_STATUS_READY:
                                self._episode_repo.update_still_enrich_state(
                                    session,
                                    show_trakt_id,
                                    season,
                                    episode,
                                    status=ENRICH_STATUS_READY,
                                    still_url=still_url,
                                )
                                changed = True
                            continue
                        self._episode_repo.update_still_enrich_state(
                            session,
                            show_trakt_id,
                            season,
                            episode,
                            status=ENRICH_STATUS_CHECKED_NO_DATA,
                            still_url="",
                        )
        return changed

    def _set_still_states(
        self,
        show_trakt_id: int,
        episodes: list[tuple[int, int]],
        *,
        status: str,
    ) -> None:
        """Apply a provider result in one short transaction, never during I/O."""
        with self._db.session() as session:
            for season, episode in episodes:
                update = {"status": status}
                if status == ENRICH_STATUS_CHECKED_NO_DATA:
                    update["still_url"] = ""
                self._episode_repo.update_still_enrich_state(
                    session,
                    show_trakt_id,
                    season,
                    episode,
                    **update,
                )

    @staticmethod
    def _season_still_urls(tmdb, show_tmdb_id: int, season: int, episodes: list[int]) -> dict[int, str]:
        load_season_stills = getattr(tmdb, "get_season_episode_still_urls", None)
        if callable(load_season_stills):
            return load_season_stills(show_tmdb_id, season)
        return {
            episode: tmdb.get_episode_still_url(show_tmdb_id, season, episode)
            for episode in episodes
        }

    def force_refresh_show_stills(self, show_trakt_id: int) -> bool:
        with self._db.session() as session:
            rows = self._episode_repo.list_show_episode_metadata(session, show_trakt_id)
        keys = [
            (int(show_trakt_id), int(row["season"]), int(row["number"]))
            for row in rows
            if row.get("season") is not None and row.get("number") is not None
        ]
        if not keys:
            return False
        return self.enrich_episode_stills(
            keys,
            trigger=TRIGGER_MANUAL_REPAIR,
            requested_parts=(ASSET_KIND_STILL,),
        )

    @staticmethod
    def should_refresh_next_episode_details(next_episode: EpisodeSummary, cached_row) -> bool:
        if cached_row is None:
            return True
        if cached_row.episode_trakt_id != next_episode.trakt_id:
            return True
        if cached_row.title != next_episode.title:
            return True
        if cached_row.first_aired != next_episode.first_aired:
            return True
        if getattr(cached_row, "trakt_details_status", ENRICH_STATUS_UNKNOWN) in {
            ENRICH_STATUS_READY,
            ENRICH_STATUS_CHECKED_NO_DATA,
        }:
            return False
        if cached_row.trakt_rating is None or cached_row.trakt_votes is None:
            return True
        if not cached_row.imdb_id:
            return True
        return False

    def _load_show_tmdb_id(self, show_trakt_id: int, client) -> int | None:
        if self._titles is not None:
            with self._db.session() as session:
                title_row = self._titles.get_title(session, show_trakt_id)
                if title_row is not None and getattr(title_row, "tmdb_id", None):
                    return int(title_row.tmdb_id)
        title = client.get_title_details(show_trakt_id, "show")
        if self._titles is not None and title.tmdb_id:
            with self._db.session() as session:
                self._titles.upsert_title(session, title)
        return title.tmdb_id
