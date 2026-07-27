from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import RLock

from trakt_tracker.application.episode_imdb_resolver import EpisodeIMDbResolver


IMDB_MATCH_STATUS_UNKNOWN = "unknown"
IMDB_MATCH_STATUS_RESOLVED = "resolved"
IMDB_MATCH_STATUS_RESOLVED_NO_RATING = "resolved_no_rating"
IMDB_MATCH_STATUS_ALTERNATE_PARENT = "alternate_parent"
IMDB_MATCH_STATUS_NO_MATCH = "no_match"
_TERMINAL_KNOWN_ID_STATUSES = {
    IMDB_MATCH_STATUS_RESOLVED,
    IMDB_MATCH_STATUS_RESOLVED_NO_RATING,
    IMDB_MATCH_STATUS_ALTERNATE_PARENT,
}


def imdb_match_status_for_resolution(resolution) -> str:
    if resolution.is_alternate_parent:
        return IMDB_MATCH_STATUS_ALTERNATE_PARENT
    if resolution.imdb_rating is None or resolution.imdb_votes is None:
        return IMDB_MATCH_STATUS_RESOLVED_NO_RATING
    return IMDB_MATCH_STATUS_RESOLVED


@dataclass(frozen=True, slots=True)
class EpisodeIMDbReconciliationResult:
    attempted: int = 0
    resolved: int = 0
    no_match: int = 0
    changed: int = 0


class EpisodeIMDbReconciliationService:
    """Resolve only episode mappings whose persisted inputs are new or stale."""

    def __init__(self, db, episode_repo, imdb_client) -> None:
        self._db = db
        self._episode_repo = episode_repo
        self._imdb_client = imdb_client
        self._resolver = EpisodeIMDbResolver(imdb_client)
        self._lock = RLock()

    def needs_reconciliation(
        self,
        *,
        show_imdb_id: str,
        episode_rows: list[dict],
        force: bool = False,
        refresh_known: bool = False,
    ) -> bool:
        if not show_imdb_id or not episode_rows:
            return False
        if force:
            return True
        mapping_candidates = []
        known_id_candidates = []
        for row in episode_rows:
            season = int(row.get("season") or 0)
            episode = int(row.get("number") or 0)
            if season <= 0 or episode <= 0:
                continue
            if str(row.get("imdb_id") or ""):
                status = str(row.get("imdb_match_status") or IMDB_MATCH_STATUS_UNKNOWN)
                if (
                    refresh_known
                    or status not in _TERMINAL_KNOWN_ID_STATUSES
                    or (
                        status == IMDB_MATCH_STATUS_RESOLVED
                        and (row.get("imdb_rating") is None or row.get("imdb_votes") is None)
                    )
                    or (
                        status == IMDB_MATCH_STATUS_RESOLVED
                        and (row.get("imdb_season") is None or row.get("imdb_episode") is None)
                    )
                ):
                    known_id_candidates.append(row)
                continue
            mapping_candidates.append((row, season, episode))
        if not mapping_candidates and not known_id_candidates:
            return False
        revision = self._dataset_revision()
        if not revision:
            return False
        if refresh_known and known_id_candidates:
            return True
        for row in known_id_candidates:
            status = str(row.get("imdb_match_status") or IMDB_MATCH_STATUS_UNKNOWN)
            if str(row.get("imdb_coordinates_revision") or "") != revision:
                return True
            if status not in _TERMINAL_KNOWN_ID_STATUSES:
                return True
            if (
                status == IMDB_MATCH_STATUS_RESOLVED
                and (row.get("imdb_season") is None or row.get("imdb_episode") is None)
            ):
                return True
            if (
                status == IMDB_MATCH_STATUS_RESOLVED
                and (row.get("imdb_rating") is None or row.get("imdb_votes") is None)
            ):
                return True
        for row, season, episode in mapping_candidates:
            attempt_key = self._attempt_key(
                revision=revision,
                show_imdb_id=show_imdb_id,
                season=season,
                episode=episode,
                title=str(row.get("title") or ""),
            )
            if (
                str(row.get("imdb_match_status") or IMDB_MATCH_STATUS_UNKNOWN) != IMDB_MATCH_STATUS_NO_MATCH
                or str(row.get("imdb_match_attempt_key") or "") != attempt_key
            ):
                return True
        return False

    def reconcile_show(
        self,
        show_trakt_id: int,
        *,
        show_imdb_id: str,
        force: bool = False,
        refresh_known: bool = False,
    ) -> EpisodeIMDbReconciliationResult:
        revision = self._dataset_revision()
        if not show_imdb_id or not revision:
            return EpisodeIMDbReconciliationResult()
        attempted = resolved = no_match = changed = 0
        with self._lock:
            with self._db.session() as session:
                rows = self._episode_repo.list_show_episodes(session, show_trakt_id)
                for row in rows:
                    season = int(row.season or 0)
                    episode = int(row.number or 0)
                    if season <= 0 or episode <= 0:
                        continue
                    if row.imdb_id and not force:
                        status = str(row.imdb_match_status or IMDB_MATCH_STATUS_UNKNOWN)
                        if (
                            not refresh_known
                            and row.imdb_coordinates_revision == revision
                            and status in _TERMINAL_KNOWN_ID_STATUSES
                            and not (
                                status == IMDB_MATCH_STATUS_RESOLVED
                                and (
                                    row.imdb_rating is None
                                    or row.imdb_votes is None
                                    or row.imdb_season is None
                                    or row.imdb_episode is None
                                )
                            )
                        ):
                            continue
                        previous = (
                            row.imdb_id,
                            row.imdb_rating,
                            row.imdb_votes,
                            row.imdb_match_status,
                            row.imdb_match_attempt_key,
                            row.imdb_season,
                            row.imdb_episode,
                            row.imdb_coordinates_revision,
                        )
                        attempted += 1
                        resolution = self._resolver.resolve_known_id(
                            show_imdb_id=show_imdb_id,
                            title=row.title,
                            imdb_id=row.imdb_id,
                        )
                        row.imdb_id = resolution.imdb_id or row.imdb_id
                        row.imdb_rating = resolution.imdb_rating
                        row.imdb_votes = resolution.imdb_votes
                        row.imdb_season = resolution.imdb_season
                        row.imdb_episode = resolution.imdb_episode
                        row.imdb_match_status = imdb_match_status_for_resolution(resolution)
                        row.imdb_match_attempt_key = ""
                        row.imdb_coordinates_revision = revision
                        current = (
                            row.imdb_id,
                            row.imdb_rating,
                            row.imdb_votes,
                            row.imdb_match_status,
                            row.imdb_match_attempt_key,
                            row.imdb_season,
                            row.imdb_episode,
                            row.imdb_coordinates_revision,
                        )
                        if current != previous:
                            changed += 1
                        continue
                    attempt_key = self._attempt_key(
                        revision=revision,
                        show_imdb_id=show_imdb_id,
                        season=season,
                        episode=episode,
                        title=row.title,
                    )
                    if (
                        not force
                        and row.imdb_match_status == IMDB_MATCH_STATUS_NO_MATCH
                        and row.imdb_match_attempt_key == attempt_key
                    ):
                        continue
                    attempted += 1
                    resolution = self._resolver.resolve(
                        show_imdb_id=show_imdb_id,
                        season=season,
                        episode=episode,
                        title=row.title,
                        trakt_imdb_id=row.imdb_id,
                    )
                    previous = (
                        row.imdb_id,
                        row.imdb_rating,
                        row.imdb_votes,
                        row.imdb_match_status,
                        row.imdb_match_attempt_key,
                        row.imdb_season,
                        row.imdb_episode,
                        row.imdb_coordinates_revision,
                    )
                    row.imdb_id = resolution.imdb_id
                    row.imdb_rating = resolution.imdb_rating
                    row.imdb_votes = resolution.imdb_votes
                    row.imdb_season = resolution.imdb_season
                    row.imdb_episode = resolution.imdb_episode
                    if resolution.imdb_id:
                        row.imdb_match_status = imdb_match_status_for_resolution(resolution)
                        row.imdb_match_attempt_key = ""
                        row.imdb_coordinates_revision = revision
                        resolved += 1
                    else:
                        row.imdb_match_status = IMDB_MATCH_STATUS_NO_MATCH
                        row.imdb_match_attempt_key = attempt_key
                        row.imdb_coordinates_revision = ""
                        no_match += 1
                    current = (
                        row.imdb_id,
                        row.imdb_rating,
                        row.imdb_votes,
                        row.imdb_match_status,
                        row.imdb_match_attempt_key,
                        row.imdb_season,
                        row.imdb_episode,
                        row.imdb_coordinates_revision,
                    )
                    if current != previous:
                        changed += 1
        return EpisodeIMDbReconciliationResult(
            attempted=attempted,
            resolved=resolved,
            no_match=no_match,
            changed=changed,
        )

    def _dataset_revision(self) -> str:
        if not self._imdb_client.is_ready():
            return ""
        revision = getattr(self._imdb_client, "dataset_revision", None)
        return str(revision() if callable(revision) else "ready")

    @staticmethod
    def _attempt_key(*, revision: str, show_imdb_id: str, season: int, episode: int, title: str) -> str:
        normalized_title = " ".join(str(title or "").strip().casefold().split())
        raw = f"{revision}\0{show_imdb_id}\0{season}\0{episode}\0{normalized_title}"
        return sha256(raw.encode("utf-8")).hexdigest()
