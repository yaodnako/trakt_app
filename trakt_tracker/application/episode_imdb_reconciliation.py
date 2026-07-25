from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import RLock

from trakt_tracker.application.episode_imdb_resolver import EpisodeIMDbResolver


IMDB_MATCH_STATUS_UNKNOWN = "unknown"
IMDB_MATCH_STATUS_RESOLVED = "resolved"
IMDB_MATCH_STATUS_NO_MATCH = "no_match"


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
    ) -> bool:
        if not show_imdb_id or not episode_rows:
            return False
        mapping_candidates = []
        coordinate_candidates = []
        for row in episode_rows:
            season = int(row.get("season") or 0)
            episode = int(row.get("number") or 0)
            if season <= 0 or episode <= 0:
                continue
            if force:
                mapping_candidates.append((row, season, episode))
                continue
            if str(row.get("imdb_id") or ""):
                if row.get("imdb_season") is None or row.get("imdb_episode") is None:
                    coordinate_candidates.append(row)
                continue
            mapping_candidates.append((row, season, episode))
        if not mapping_candidates and not coordinate_candidates:
            return False
        revision = self._dataset_revision()
        if not revision:
            return False
        if force:
            return True
        if any(str(row.get("imdb_coordinates_revision") or "") != revision for row in coordinate_candidates):
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

    def reconcile_show(self, show_trakt_id: int, *, show_imdb_id: str, force: bool = False) -> EpisodeIMDbReconciliationResult:
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
                        previous = (
                            row.imdb_match_status,
                            row.imdb_match_attempt_key,
                            row.imdb_season,
                            row.imdb_episode,
                            row.imdb_coordinates_revision,
                        )
                        if row.imdb_match_status != IMDB_MATCH_STATUS_RESOLVED or row.imdb_match_attempt_key:
                            row.imdb_match_status = IMDB_MATCH_STATUS_RESOLVED
                            row.imdb_match_attempt_key = ""
                        if (
                            (row.imdb_season is None or row.imdb_episode is None)
                            and row.imdb_coordinates_revision != revision
                        ):
                            attempted += 1
                            row.imdb_season, row.imdb_episode = self._lookup_coordinates(
                                row.imdb_id,
                                show_imdb_id=show_imdb_id,
                            )
                            row.imdb_coordinates_revision = revision
                        current = (
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
                        row.imdb_match_status = IMDB_MATCH_STATUS_RESOLVED
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

    def _lookup_coordinates(self, imdb_id: str, *, show_imdb_id: str) -> tuple[int | None, int | None]:
        lookup = getattr(self._imdb_client, "lookup_episode_metadata", None)
        metadata = lookup(imdb_id) if callable(lookup) else None
        if not isinstance(metadata, dict):
            return None, None
        parent_imdb_id = str(metadata.get("parent_imdb_id") or "")
        if parent_imdb_id and parent_imdb_id != show_imdb_id:
            return None, None
        return self._positive_int(metadata.get("season")), self._positive_int(metadata.get("episode"))

    @staticmethod
    def _positive_int(value) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _attempt_key(*, revision: str, show_imdb_id: str, season: int, episode: int, title: str) -> str:
        normalized_title = " ".join(str(title or "").strip().casefold().split())
        raw = f"{revision}\0{show_imdb_id}\0{season}\0{episode}\0{normalized_title}"
        return sha256(raw.encode("utf-8")).hexdigest()
