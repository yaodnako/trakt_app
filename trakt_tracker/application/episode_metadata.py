from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

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
        auth_service=None,
        tmdb_factory: Callable[[AppConfig], TMDbClient] | None = None,
    ) -> None:
        self._db = db
        self._episode_repo = episode_repo
        self._imdb_client = imdb_client
        self._auth = auth_service
        self._tmdb_factory = tmdb_factory

    def load_cached_trakt_rating_maps(self) -> tuple[dict[int, int], dict[tuple[int, int, int], int]]:
        title_ratings: dict[int, tuple[datetime, int]] = {}
        episode_ratings: dict[tuple[int, int, int], tuple[datetime, int]] = {}
        for item in load_cached_trakt_rating_items():
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
        payloads = load_cached_trakt_history_items() + load_cached_trakt_rating_items()
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
            imdb_id = str(episode_ids.get("imdb", "") or "")
            if not imdb_id:
                show_imdb_id = str(show_ids.get("imdb", "") or "")
                season_number = episode_payload.get("season")
                episode_number = episode_payload.get("number")
                if show_imdb_id and season_number is not None and episode_number is not None:
                    imdb_id = self._imdb_client.lookup_episode_imdb_id(show_imdb_id, int(season_number), int(episode_number))
                if not imdb_id and show_imdb_id:
                    imdb_id = self._imdb_client.lookup_episode_imdb_id_by_title(
                        show_imdb_id,
                        str(episode_payload.get("title", "") or ""),
                    )
            if not imdb_id:
                continue
            episode = EpisodeSummary(
                trakt_id=episode_ids.get("trakt", 0),
                season=episode_payload.get("season", 0),
                number=episode_payload.get("number", 0),
                title=episode_payload.get("title", ""),
                imdb_id=imdb_id,
            )
            enriched = self._imdb_client.enrich_episode(episode)
            result[key] = {
                "imdb_id": imdb_id,
                "imdb_rating": enriched.imdb_rating,
                "imdb_votes": enriched.imdb_votes,
            }
        return result

    def enrich_episode_imdb_ratings(self) -> None:
        if not self._imdb_client.is_ready():
            return
        with self._db.session() as session:
            rows = self._episode_repo.list_all_with_imdb(session)
            for row in rows:
                if not row.imdb_id:
                    continue
                episode = EpisodeSummary(
                    trakt_id=row.episode_trakt_id,
                    season=row.season,
                    number=row.number,
                    title=row.title,
                    imdb_id=row.imdb_id,
                    imdb_rating=row.imdb_rating,
                    imdb_votes=row.imdb_votes,
                    first_aired=row.first_aired,
                    runtime=row.runtime,
                    overview=row.overview,
                )
                enriched = self._imdb_client.enrich_episode(episode)
                row.imdb_rating = enriched.imdb_rating
                row.imdb_votes = enriched.imdb_votes

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
                imdb_id = str(episode_ids.get("imdb", "") or "")
                if not imdb_id:
                    show_imdb_id = str(show_ids.get("imdb", "") or "")
                    if show_imdb_id:
                        imdb_id = self._imdb_client.lookup_episode_imdb_id(show_imdb_id, int(season), int(number))
                        if not imdb_id:
                            imdb_id = self._imdb_client.lookup_episode_imdb_id_by_title(
                                show_imdb_id,
                                str(episode_payload.get("title", "") or ""),
                            )
                if imdb_id and not row.imdb_id:
                    row.imdb_id = imdb_id

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
            enriched = self._imdb_client.enrich_episode(
                EpisodeSummary(
                    trakt_id=row.episode_trakt_id,
                    season=row.season,
                    number=row.number,
                    title=row.title,
                    imdb_id=row.imdb_id,
                    imdb_rating=row.imdb_rating,
                    imdb_votes=row.imdb_votes,
                    first_aired=row.first_aired,
                    runtime=row.runtime,
                    overview=row.overview,
                )
            )
            row.imdb_rating = enriched.imdb_rating
            row.imdb_votes = enriched.imdb_votes
        progress.next_episode.still_url = row.still_url or ""
        progress.next_episode.imdb_rating = row.imdb_rating
        progress.next_episode.imdb_votes = row.imdb_votes

    def can_enrich_episode_stills(self) -> bool:
        if self._auth is None or self._tmdb_factory is None:
            return False
        return self._tmdb_factory(self._auth.config).is_configured()

    def enrich_episode_stills(self, keys: list[tuple[int, int, int]]) -> bool:
        if not keys or not self.can_enrich_episode_stills():
            return False
        unique_keys = list(dict.fromkeys(keys))
        with self._db.session() as session:
            metadata = self._episode_repo.metadata_by_episode_keys(session, unique_keys)
        missing_by_show: dict[int, list[tuple[int, int]]] = {}
        for show_trakt_id, season, episode in unique_keys:
            if (metadata.get((show_trakt_id, season, episode)) or {}).get("still_url"):
                continue
            missing_by_show.setdefault(show_trakt_id, []).append((season, episode))
        if not missing_by_show:
            return False
        client = self._auth.get_client()
        tmdb = self._tmdb_factory(self._auth.config)
        show_tmdb_ids = {
            show_trakt_id: self._load_show_tmdb_id(client, show_trakt_id)
            for show_trakt_id in missing_by_show
        }
        changed = False
        with self._db.session() as session:
            for show_trakt_id, episodes in missing_by_show.items():
                show_tmdb_id = show_tmdb_ids.get(show_trakt_id)
                if not show_tmdb_id:
                    continue
                for season, episode in episodes:
                    try:
                        still_url = tmdb.get_episode_still_url(show_tmdb_id, season, episode)
                    except Exception:
                        continue
                    if not still_url:
                        continue
                    row = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
                    if row is None or row.still_url == still_url:
                        continue
                    row.still_url = still_url
                    changed = True
        return changed

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
        if cached_row.trakt_rating is None or cached_row.trakt_votes is None:
            return True
        if not cached_row.imdb_id:
            return True
        return False

    @staticmethod
    def _load_show_tmdb_id(client, show_trakt_id: int) -> int | None:
        try:
            title = client.get_title_details(show_trakt_id, "show")
        except Exception:
            return None
        return title.tmdb_id
