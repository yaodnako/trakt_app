from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select

from trakt_tracker.config import normalize_catalog_provider_mode
from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.application.trakt_outbox import TraktOutboxService
from trakt_tracker.domain import EpisodeSummary, ProgressSnapshot, TitleSummary, synthetic_episode_id
from trakt_tracker.infrastructure.tmdb import (
    TMDB_BACKDROP_IMAGE_BASE,
    TMDB_POSTER_IMAGE_BASE,
    TMDbClient,
)
from trakt_tracker.infrastructure.notifications import NotificationMessage
from trakt_tracker.persistence.models import (
    EpisodeCache,
    HistoryEvent,
    ReleaseTrackingState,
    Title,
    UserTitleState,
    WatchProgress,
)
from trakt_tracker.persistence.repositories import SyncStateRepository
from trakt_tracker.persistence.tmdb_preview import TmdbPreviewRepository


TMDB_PREVIEW_SEARCH_STATE_KEY = "tmdb_preview_search_state"
TMDB_PREVIEW_EXPLORE_STATE_KEY = "tmdb_preview_explore_state"
TMDB_LOCAL_STATE_PROJECTION_KEY = "tmdb_local_state_projection_v4"


@dataclass(slots=True)
class TmdbCatalogItem:
    provider: str = "tmdb"
    title_type: str = "movie"
    tmdb_id: int = 0
    trakt_id: int | None = None
    imdb_id: str = ""
    title: str = ""
    year: int | None = None
    overview: str = ""
    status: str = ""
    slug: str = ""
    poster_url: str = ""
    backdrop_url: str = ""
    tmdb_rating: float | None = None
    tmdb_votes: int | None = None
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    ratings_status: str = ENRICH_STATUS_UNKNOWN
    popularity: float | None = None
    released_at: datetime | None = None
    explore_metric_kind: str = ""
    explore_metric_count: int | None = None
    explore_rank: int | None = None
    release_distance_text: str = ""
    is_watchlisted: bool = False
    is_in_history: bool = False
    is_release_tracked: bool = False
    release_acknowledged: bool = False
    is_future_release: bool = False
    is_notification_matured: bool = False
    local_only: bool = True
    catalog_actions_available: bool = True
    seasons: list[dict[str, Any]] = field(default_factory=list)
    title_episode_avg_rating: float | None = None

    @property
    def tmdb_url(self) -> str:
        media = "tv" if self.title_type == "show" else "movie"
        return f"https://www.themoviedb.org/{media}/{int(self.tmdb_id)}"

    @property
    def source_url(self) -> str:
        return self.tmdb_url

    @property
    def identity_key(self) -> tuple[str, int]:
        return self.title_type, int(self.tmdb_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "title_type": self.title_type,
            "tmdb_id": int(self.tmdb_id),
            "trakt_id": int(self.trakt_id) if self.trakt_id else None,
            "imdb_id": self.imdb_id,
            "title": self.title,
            "year": self.year,
            "overview": self.overview,
            "status": self.status,
            "slug": self.slug,
            "poster_url": self.poster_url,
            "backdrop_url": self.backdrop_url,
            "tmdb_rating": self.tmdb_rating,
            "tmdb_votes": self.tmdb_votes,
            "imdb_rating": self.imdb_rating,
            "imdb_votes": self.imdb_votes,
            "ratings_status": self.ratings_status,
            "popularity": self.popularity,
            "released_at": self.released_at.isoformat() if self.released_at else "",
            "seasons": _snapshot_seasons(self.seasons),
        }


@dataclass(slots=True)
class TmdbCatalogPage:
    items: list[TmdbCatalogItem]
    page: int
    page_count: int


class TmdbCatalogService:
    """TMDb catalog plus profile-local state.

    TMDb mode never lets a historical Trakt mapping select data, mutate a
    legacy service, or enqueue Trakt work. Trakt mode retains its existing
    mapping/export behavior.
    """

    def __init__(
        self,
        db,
        auth_service,
        tmdb_factory: Callable[[Any], TMDbClient],
        titles,
        preview_repository: TmdbPreviewRepository,
        trakt_outbox: TraktOutboxService | None = None,
        imdb_client=None,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._tmdb_factory = tmdb_factory
        self._titles = titles
        self._repository = preview_repository
        self._sync_state = SyncStateRepository()
        self._trakt_outbox = trakt_outbox
        self._imdb_client = imdb_client
        self._legacy_catalog = None
        self._legacy_release_tracking = None
        self._legacy_search_watch = None
        self._notification_sender = None

    def set_legacy_services(self, *, catalog=None, release_tracking=None, search_watch=None) -> None:
        self._legacy_catalog = catalog
        self._legacy_release_tracking = release_tracking
        self._legacy_search_watch = search_watch

    def is_configured(self) -> bool:
        return self._tmdb_factory(self._auth.config).is_configured()

    def episode_schedule_enabled(self) -> bool:
        return self._tmdb_local_mode()

    def _tmdb_local_mode(self) -> bool:
        return normalize_catalog_provider_mode(
            getattr(self._auth.config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview"

    def _mapped_trakt_id_for_mutation(self, item: TmdbCatalogItem) -> int | None:
        if self._tmdb_local_mode():
            return None
        return int(item.trakt_id or 0) or None

    def _apply_mode_boundary(self, item: TmdbCatalogItem) -> TmdbCatalogItem:
        if self._tmdb_local_mode():
            item.trakt_id = None
            item.local_only = True
        else:
            item.local_only = item.trakt_id is None
        return item

    def _ensure_local_state_projection(self) -> None:
        """Copy existing on-device state once before TMDb mode reads it.

        The source tables are local SQLite rows accumulated while Trakt mode was
        active.  This deliberately performs no service, client, outbox, or
        network call: after the transaction, TMDb mode reads only the TMDb
        local projection.
        """
        if not self._tmdb_local_mode():
            return
        with self._db.session() as session:
            if self._sync_state.get_value(session, TMDB_LOCAL_STATE_PROJECTION_KEY, "") == "complete":
                return
            self._project_legacy_local_state(session)
            self._sync_state.set_value(session, TMDB_LOCAL_STATE_PROJECTION_KEY, "complete")

    def _project_legacy_local_state(self, session) -> None:
        titles = [
            title
            for title in session.scalars(select(Title).where(Title.tmdb_id.is_not(None)))
            if title.title_type in {"movie", "show"} and int(title.tmdb_id or 0) > 0
        ]
        titles_by_trakt_id = {int(title.trakt_id): title for title in titles}
        titles_by_row_id = {int(title.id): title for title in titles}
        events = list(session.scalars(select(HistoryEvent).order_by(HistoryEvent.watched_at, HistoryEvent.id)))
        progress_rows = list(session.scalars(select(WatchProgress)))
        progress_by_trakt_id = {int(progress.show_trakt_id): progress for progress in progress_rows}
        release_rows = list(session.scalars(select(ReleaseTrackingState)))
        user_states = list(session.execute(select(Title, UserTitleState).join(UserTitleState)))
        watchlist_keys = self._legacy_watchlist_keys(session)
        progress_trakt_ids = {int(progress.show_trakt_id) for progress in progress_rows}

        relevant_trakt_ids: set[int] = set()
        for event in events:
            if int(event.title_trakt_id) in titles_by_trakt_id:
                relevant_trakt_ids.add(int(event.title_trakt_id))
        for progress in progress_rows:
            if int(progress.show_trakt_id) in titles_by_trakt_id:
                relevant_trakt_ids.add(int(progress.show_trakt_id))
        for release in release_rows:
            if int(release.trakt_id) in titles_by_trakt_id:
                relevant_trakt_ids.add(int(release.trakt_id))
        for title, state in user_states:
            if int(title.trakt_id) in titles_by_trakt_id and (
                bool(state.in_history)
                or bool(state.archived)
                or (state.tracked is False and int(title.trakt_id) in progress_trakt_ids)
                or bool(state.paused)
                or state.rating is not None
            ):
                relevant_trakt_ids.add(int(title.trakt_id))
        for _title_type, trakt_id in watchlist_keys:
            if trakt_id in titles_by_trakt_id:
                relevant_trakt_ids.add(trakt_id)

        episode_rows_by_show: dict[int, list[EpisodeCache]] = {}
        show_ids = {
            trakt_id
            for trakt_id in relevant_trakt_ids
            if titles_by_trakt_id[trakt_id].title_type == "show"
        }
        if show_ids:
            for row in session.scalars(
                select(EpisodeCache)
                .where(EpisodeCache.show_trakt_id.in_(show_ids))
                .order_by(EpisodeCache.show_trakt_id, EpisodeCache.season, EpisodeCache.number)
            ):
                episode_rows_by_show.setdefault(int(row.show_trakt_id), []).append(row)

        snapshots: dict[int, dict[str, Any]] = {}
        for trakt_id in relevant_trakt_ids:
            title = titles_by_trakt_id[trakt_id]
            payload = self._legacy_title_snapshot(
                title,
                episodes=episode_rows_by_show.get(trakt_id, []),
                progress=progress_by_trakt_id.get(trakt_id),
            )
            snapshot = self._repository.snapshot(session, title.title_type, int(title.tmdb_id))
            if snapshot is not None:
                existing = _json_dict(snapshot.payload_json)
                if existing:
                    preserved = {
                        key: value
                        for key, value in existing.items()
                        if key not in {"provider", "trakt_id", "seasons", "local_progress", "local_progress_dirty"}
                    }
                    payload = {**payload, **preserved}
                    if not payload.get("seasons") and existing.get("seasons"):
                        payload["seasons"] = [
                            _sanitize_snapshot_season(value)
                            for value in existing.get("seasons", [])
                            if isinstance(value, dict)
                        ]
                    if bool(existing.get("local_progress_dirty")):
                        payload["local_progress_dirty"] = True
            payload["trakt_id"] = None
            payload["provider"] = "tmdb"
            self._repository.upsert_snapshot(session, payload)
            snapshots[trakt_id] = payload

        episode_coordinates = {
            (int(row.show_trakt_id), int(row.season), int(row.number)): self._legacy_episode_coordinates(row)
            for rows in episode_rows_by_show.values()
            for row in rows
        }
        latest_history: dict[tuple[str, int, int | None, int | None], HistoryEvent] = {}
        latest_ratings: dict[tuple[str, int, int | None, int | None], HistoryEvent] = {}
        for event in events:
            title = titles_by_trakt_id.get(int(event.title_trakt_id))
            if title is None or int(title.trakt_id) not in relevant_trakt_ids:
                continue
            season, episode = self._projected_event_coordinates(event, episode_coordinates)
            key = (title.title_type, int(title.tmdb_id), season, episode)
            if event.action == "watched":
                latest_history[key] = event
            rating = _optional_int(event.rating)
            if rating is not None and 1 <= rating <= 10 and event.action in {"watched", "rated"}:
                latest_ratings[key] = event

        for (title_type, tmdb_id, season, episode), event in latest_history.items():
            if self._repository.intent(
                session,
                operation_type="history",
                title_type=title_type,
                tmdb_id=tmdb_id,
                season=season,
                episode=episode,
            ) is None:
                source_title = titles_by_trakt_id[int(event.title_trakt_id)]
                self._repository.set_intent(
                    session,
                    operation_type="history",
                    title_type=title_type,
                    tmdb_id=tmdb_id,
                    desired=True,
                    season=season,
                    episode=episode,
                    payload={
                        **snapshots[int(source_title.trakt_id)],
                        "watched_at": _legacy_datetime_text(event.watched_at),
                        "season": season,
                        "episode": episode,
                    },
                )
        for (title_type, tmdb_id, season, episode), event in latest_ratings.items():
            if self._repository.intent(
                session,
                operation_type="rating",
                title_type=title_type,
                tmdb_id=tmdb_id,
                season=season,
                episode=episode,
            ) is None:
                source_title = titles_by_trakt_id[int(event.title_trakt_id)]
                self._repository.set_value_intent(
                    session,
                    operation_type="rating",
                    title_type=title_type,
                    tmdb_id=tmdb_id,
                    desired=int(event.rating),
                    season=season,
                    episode=episode,
                    payload={
                        **snapshots[int(source_title.trakt_id)],
                        "rating": int(event.rating),
                        "season": season,
                        "episode": episode,
                    },
                )

        for title_type, trakt_id in watchlist_keys:
            title = titles_by_trakt_id.get(trakt_id)
            if title is None or title.title_type != title_type:
                continue
            if self._repository.intent(
                session,
                operation_type="watchlist",
                title_type=title.title_type,
                tmdb_id=int(title.tmdb_id),
            ) is None:
                self._repository.set_intent(
                    session,
                    operation_type="watchlist",
                    title_type=title.title_type,
                    tmdb_id=int(title.tmdb_id),
                    desired=True,
                    payload=snapshots[int(title.trakt_id)],
                )

        history_title_keys = {(title_type, tmdb_id) for title_type, tmdb_id, _season, _episode in latest_history}
        for title, state in user_states:
            mapped = titles_by_row_id.get(int(title.id))
            if mapped is None or int(mapped.trakt_id) not in relevant_trakt_ids:
                continue
            tmdb_id = int(mapped.tmdb_id)
            if bool(state.in_history) and (mapped.title_type, tmdb_id) not in history_title_keys:
                if self._repository.intent(
                    session,
                    operation_type="history",
                    title_type=mapped.title_type,
                    tmdb_id=tmdb_id,
                ) is None:
                    self._repository.set_intent(
                        session,
                        operation_type="history",
                        title_type=mapped.title_type,
                        tmdb_id=tmdb_id,
                        desired=True,
                        payload={
                            **snapshots[int(mapped.trakt_id)],
                            "watched_at": _legacy_datetime_text(state.last_watched_at),
                        },
                    )
            if state.rating is not None and 1 <= int(state.rating) <= 10:
                if self._repository.intent(
                    session,
                    operation_type="rating",
                    title_type=mapped.title_type,
                    tmdb_id=tmdb_id,
                ) is None:
                    self._repository.set_value_intent(
                        session,
                        operation_type="rating",
                        title_type=mapped.title_type,
                        tmdb_id=tmdb_id,
                        desired=int(state.rating),
                        payload={**snapshots[int(mapped.trakt_id)], "rating": int(state.rating)},
                    )
            if bool(state.paused):
                self._set_projected_flag(session, "pause", mapped, desired=True, payload=snapshots[int(mapped.trakt_id)])
            if bool(state.archived) or (
                state.tracked is False and int(mapped.trakt_id) in progress_trakt_ids
            ):
                self._set_projected_flag(session, "drop", mapped, desired=True, payload=snapshots[int(mapped.trakt_id)])

        for release in release_rows:
            title = titles_by_trakt_id.get(int(release.trakt_id))
            if title is None or int(title.trakt_id) not in relevant_trakt_ids:
                continue
            state = self._repository.upsert_release_state(
                session,
                title_type=title.title_type,
                tmdb_id=int(title.tmdb_id),
                title=str(release.title or title.title),
                release_at=_as_utc(release.release_at),
                list_count=release.list_count,
            )
            state.acknowledged_at = release.acknowledged_at
            state.last_sent_at = release.last_sent_at
            state.notify_count = int(release.notify_count or 0)

        self._project_progress_history(
            session,
            progress_rows=progress_rows,
            titles_by_trakt_id=titles_by_trakt_id,
            snapshots=snapshots,
            episode_rows_by_show=episode_rows_by_show,
        )

    def _legacy_watchlist_keys(self, session) -> set[tuple[str, int]]:
        raw = self._sync_state.get_value(session, "watchlist_snapshot_v1", "")
        payload = _json_dict(raw)
        values = payload.get("keys", []) if isinstance(payload, dict) else []
        result: set[tuple[str, int]] = set()
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            title_type = str(value[0] or "")
            trakt_id = _optional_int(value[1])
            if title_type in {"movie", "show"} and trakt_id is not None and trakt_id > 0:
                result.add((title_type, trakt_id))
        return result

    def _legacy_title_snapshot(
        self,
        title: Title,
        *,
        episodes: list[EpisodeCache],
        progress: WatchProgress | None = None,
    ) -> dict[str, Any]:
        payload = {
            "provider": "tmdb",
            "title_type": str(title.title_type),
            "tmdb_id": int(title.tmdb_id),
            "trakt_id": None,
            "imdb_id": str(title.imdb_id or ""),
            "title": str(title.title or ""),
            "year": title.year,
            "overview": str(title.overview or ""),
            "status": str(title.status or ""),
            "slug": "",
            "poster_url": _tmdb_local_asset_url(title.poster_url),
            "backdrop_url": _tmdb_local_asset_url(title.backdrop_url),
            "tmdb_rating": _optional_float(title.tmdb_rating),
            "tmdb_votes": _optional_int(title.tmdb_votes),
            "imdb_rating": _optional_float(title.imdb_rating),
            "imdb_votes": _optional_int(title.imdb_votes),
            "ratings_status": str(title.ratings_status or ENRICH_STATUS_UNKNOWN),
            "seasons": self._legacy_episode_snapshot_seasons(episodes),
        }
        if progress is not None:
            payload["local_progress"] = self._legacy_progress_payload(progress, episodes)
        return payload

    @staticmethod
    def _legacy_episode_coordinates(row: EpisodeCache) -> tuple[int, int]:
        season = _optional_int(row.imdb_season)
        episode = _optional_int(row.imdb_episode)
        return (
            season if season is not None and season > 0 else int(row.season),
            episode if episode is not None and episode > 0 else int(row.number),
        )

    def _legacy_episode_snapshot_seasons(self, rows: list[EpisodeCache]) -> list[dict[str, Any]]:
        seasons: dict[int, dict[int, dict[str, Any]]] = {}
        for row in rows:
            season, episode = self._legacy_episode_coordinates(row)
            if season <= 0 or episode <= 0:
                continue
            seasons.setdefault(season, {}).setdefault(
                episode,
                {
                    "episode": episode,
                    "tmdb_season": int(row.season),
                    "tmdb_episode": int(row.number),
                    "title": str(row.title or ""),
                    "still_url": _tmdb_local_asset_url(row.still_url),
                    "imdb_rating": _optional_float(row.imdb_rating),
                    "imdb_votes": _optional_int(row.imdb_votes),
                    "first_aired": _legacy_datetime_text(row.first_aired),
                    "overview": str(row.overview or ""),
                },
            )
        return [
            {"season_number": season, "episodes": [episodes[number] for number in sorted(episodes)]}
            for season, episodes in sorted(seasons.items())
        ]

    def _legacy_progress_payload(
        self,
        progress: WatchProgress,
        episodes: list[EpisodeCache],
    ) -> dict[str, Any]:
        by_legacy_key = {
            (int(row.season), int(row.number)): row
            for row in episodes
        }

        def episode_payload(season: int | None, number: int | None, title: str, first_aired: datetime | None):
            row = by_legacy_key.get((int(season), int(number))) if season is not None and number is not None else None
            if row is not None:
                mapped_season, mapped_episode = self._legacy_episode_coordinates(row)
                return {
                    "season": mapped_season,
                    "episode": mapped_episode,
                    "tmdb_season": int(row.season),
                    "tmdb_episode": int(row.number),
                    "title": str(row.title or title or ""),
                    "still_url": _tmdb_local_asset_url(row.still_url),
                    "imdb_rating": _optional_float(row.imdb_rating),
                    "imdb_votes": _optional_int(row.imdb_votes),
                    "first_aired": _legacy_datetime_text(row.first_aired),
                    "overview": str(row.overview or ""),
                }
            if season is None or number is None:
                return None
            return {
                "season": int(season),
                "episode": int(number),
                "tmdb_season": int(season),
                "tmdb_episode": int(number),
                "title": str(title or ""),
                "first_aired": _legacy_datetime_text(first_aired),
            }

        return {
            "completed": max(0, int(progress.completed or 0)),
            "aired": max(0, int(progress.aired or 0)),
            "percent_completed": float(progress.percent_completed or 0.0),
            "next_episode": episode_payload(
                _optional_int(progress.next_episode_season),
                _optional_int(progress.next_episode_number),
                str(progress.next_episode_title or ""),
                progress.next_episode_first_aired,
            ),
            "last_episode": episode_payload(
                _optional_int(progress.last_episode_season),
                _optional_int(progress.last_episode_number),
                str(progress.last_episode_title or ""),
                progress.last_episode_first_aired,
            ),
            "last_watched_at": _legacy_datetime_text(progress.updated_at),
        }

    def _projected_event_coordinates(
        self,
        event: HistoryEvent,
        coordinates: dict[tuple[int, int, int], tuple[int, int]],
    ) -> tuple[int | None, int | None]:
        season = _optional_int(event.season)
        episode = _optional_int(event.episode)
        if event.title_type != "show" or season is None or episode is None:
            return season, episode
        return coordinates.get((int(event.title_trakt_id), season, episode), (season, episode))

    def _set_projected_flag(self, session, operation_type: str, title: Title, *, desired: bool, payload: dict[str, Any]) -> None:
        if self._repository.intent(
            session,
            operation_type=operation_type,
            title_type=title.title_type,
            tmdb_id=int(title.tmdb_id),
        ) is None:
            self._repository.set_intent(
                session,
                operation_type=operation_type,
                title_type=title.title_type,
                tmdb_id=int(title.tmdb_id),
                desired=desired,
                payload=payload,
            )

    def _project_progress_history(
        self,
        session,
        *,
        progress_rows: list[WatchProgress],
        titles_by_trakt_id: dict[int, Title],
        snapshots: dict[int, dict[str, Any]],
        episode_rows_by_show: dict[int, list[EpisodeCache]],
    ) -> None:
        for progress in progress_rows:
            trakt_id = int(progress.show_trakt_id)
            title = titles_by_trakt_id.get(trakt_id)
            if title is None or title.title_type != "show" or trakt_id not in snapshots:
                continue
            existing = [
                row
                for row in self._repository.list_intents(
                    session,
                    operation_type="history",
                    title_type="show",
                    tmdb_id=int(title.tmdb_id),
                )
                if row.season is not None and row.episode is not None and bool(_json_value(row.desired_state_json, False))
            ]
            target_completed = max(0, int(progress.completed or 0))
            if len(existing) >= target_completed:
                continue
            source_rows = [
                row
                for row in episode_rows_by_show.get(trakt_id, [])
                if int(row.season) > 0 and int(row.number) > 0
            ]
            for row in source_rows:
                if len(existing) >= target_completed:
                    break
                season, episode = self._legacy_episode_coordinates(row)
                if self._repository.intent(
                    session,
                    operation_type="history",
                    title_type="show",
                    tmdb_id=int(title.tmdb_id),
                    season=season,
                    episode=episode,
                ) is not None:
                    continue
                projected = self._repository.set_intent(
                    session,
                    operation_type="history",
                    title_type="show",
                    tmdb_id=int(title.tmdb_id),
                    desired=True,
                    season=season,
                    episode=episode,
                    payload={
                        **snapshots[trakt_id],
                        "watched_at": _legacy_datetime_text(progress.updated_at),
                        "season": season,
                        "episode": episode,
                    },
                )
                if projected is not None:
                    existing.append(projected)

    def overlay_episode_row_air_dates(
        self,
        trakt_id: int,
        rows: list[dict],
    ) -> list[dict]:
        """Apply cached TMDb dates to episode rows without network work."""
        if not self.episode_schedule_enabled() or not rows:
            return rows
        with self._db.session() as session:
            title = self._titles.by_trakt_ids(session, [int(trakt_id)]).get(
                int(trakt_id)
            )
        tmdb_id = int(getattr(title, "tmdb_id", 0) or 0)
        if tmdb_id <= 0:
            return rows
        client = self._tmdb_factory(self._auth.config)
        schedules = {
            season: self._episode_air_dates(
                client,
                tmdb_id,
                season,
                refresh_remote=False,
            )
            for season in sorted(
                {
                    int(row["season"])
                    for row in rows
                    if row.get("season") is not None and int(row["season"]) > 0
                }
            )
        }
        for row in rows:
            season = _optional_int(row.get("season"))
            number = _optional_int(row.get("number"))
            if season is None or number is None:
                continue
            tmdb_air_date = schedules.get(season, {}).get(number)
            if tmdb_air_date is not None:
                row["first_aired"] = _replace_calendar_date(
                    row.get("first_aired"),
                    tmdb_air_date,
                )
        return rows

    def overlay_episode_air_dates(self, items: list[Any]) -> list[Any]:
        """Apply cached TMDb dates without ever starting network work."""
        return self._apply_episode_air_dates(items, refresh_remote=False)

    def refresh_episode_air_dates(self, items: list[Any]) -> list[Any]:
        """Refresh TMDb dates explicitly from a background workflow."""
        return self._apply_episode_air_dates(items, refresh_remote=True)

    def _apply_episode_air_dates(self, items: list[Any], *, refresh_remote: bool) -> list[Any]:
        if not self.episode_schedule_enabled():
            return items
        candidates = [
            item
            for item in items
            if getattr(item, "next_episode", None) is not None
        ]
        if not candidates:
            return items
        with self._db.session() as session:
            mapped_titles = self._titles.by_trakt_ids(
                session,
                [int(item.trakt_id) for item in candidates],
            )
            mappings = {
                int(trakt_id): title
                for trakt_id, title in mapped_titles.items()
                if title.tmdb_id is not None and int(title.tmdb_id) > 0
            }
        client = self._tmdb_factory(self._auth.config)
        schedules: dict[tuple[int, int], dict[int, dict[str, Any]]] = {}
        for item in candidates:
            episode = item.next_episode
            title = mappings.get(int(item.trakt_id))
            if title is None:
                continue
            tmdb_id = int(title.tmdb_id)
            item.tmdb_id = tmdb_id
            item.title_tmdb_rating = title.tmdb_rating
            item.title_tmdb_votes = title.tmdb_votes
            tmdb_season = int(episode.season)
            tmdb_episode = int(episode.number)
            schedule_key = (tmdb_id, tmdb_season)
            if schedule_key not in schedules:
                schedules[schedule_key] = self._episode_metadata(
                    client,
                    tmdb_id,
                    tmdb_season,
                    refresh_remote=refresh_remote,
                )
            metadata = schedules[schedule_key].get(tmdb_episode)
            if metadata is None:
                continue
            tmdb_air_date = _parse_datetime(metadata.get("air_date"))
            if tmdb_air_date is not None:
                episode.first_aired = _replace_calendar_date(
                    episode.first_aired,
                    tmdb_air_date,
                )
            episode.tmdb_rating = _optional_float(metadata.get("vote_average"))
            episode.tmdb_votes = _optional_int(metadata.get("vote_count"))
        return items

    @staticmethod
    def _episode_air_dates(
        client,
        tmdb_id: int,
        season: int,
        *,
        refresh_remote: bool = False,
    ) -> dict[int, datetime]:
        return {
            number: parsed
            for number, raw in TmdbCatalogService._episode_metadata(
                client,
                tmdb_id,
                season,
                refresh_remote=refresh_remote,
            ).items()
            if (parsed := _parse_datetime(raw.get("air_date"))) is not None
        }

    @staticmethod
    def _episode_metadata(
        client,
        tmdb_id: int,
        season: int,
        *,
        refresh_remote: bool = False,
    ) -> dict[int, dict[str, Any]]:
        if refresh_remote:
            reader = getattr(client, "refresh_catalog_season", None)
            if not callable(reader):
                reader = getattr(client, "get_catalog_season", None)
        else:
            reader = getattr(client, "get_cached_catalog_season", None)
        if not callable(reader):
            return {}
        try:
            payload = reader(int(tmdb_id), int(season)) or {}
        except Exception:
            return {}
        return {
            int(raw.get("episode_number")): raw
            for raw in payload.get("episodes", [])
            if isinstance(raw, dict)
            and _optional_int(raw.get("episode_number")) is not None
        }

    def search_titles(
        self,
        query: str,
        title_type: str | None = None,
        *,
        page: int = 1,
        limit: int = 24,
    ) -> TmdbCatalogPage:
        self._ensure_local_state_projection()
        payload = self._tmdb_factory(self._auth.config).search_catalog(
            query,
            title_type=title_type,
            page=page,
        )
        result = self._page_from_payload(payload, title_type=title_type, page=page, limit=limit, metric_kind="")
        self._hydrate_imdb_metadata(result.items)
        self._decorate_local_state(result.items)
        return result

    def filtered_search_titles(
        self,
        query: str,
        title_type: str | None,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        tmdb_min: float | None,
        max_scan_pages: int,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
    ) -> TmdbCatalogPage:
        return self._filtered_catalog_page(
            lambda source_page: self.search_titles(
                query,
                title_type,
                page=source_page,
                limit=limit,
            ),
            page=page,
            limit=limit,
            max_scan_pages=max_scan_pages,
            imdb_min=imdb_min,
            tmdb_min=tmdb_min,
            hide_watchlisted=hide_watchlisted,
            hide_history=hide_history,
        )

    def load_search_state(self) -> dict[str, Any]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, TMDB_PREVIEW_SEARCH_STATE_KEY, "")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "query": str(payload.get("query") or ""),
            "title_type": str(payload.get("title_type") or "all"),
            "sort_mode": str(payload.get("sort_mode") or "IMDb votes"),
            "imdb_min": str(payload.get("imdb_min") or ""),
            "tmdb_min": str(payload.get("tmdb_min") or ""),
            "hide_watchlisted": bool(payload.get("hide_watchlisted", False)),
            "hide_history": bool(payload.get("hide_history", False)),
        }

    def save_search_state(
        self,
        *,
        query: str,
        title_type: str | None,
        sort_mode: str,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
        imdb_min: str = "",
        tmdb_min: str = "",
    ) -> None:
        payload = {
            "query": str(query or "").strip(),
            "title_type": title_type or "all",
            "sort_mode": str(sort_mode or "IMDb votes"),
            "imdb_min": str(imdb_min or ""),
            "tmdb_min": str(tmdb_min or ""),
            "hide_watchlisted": bool(hide_watchlisted),
            "hide_history": bool(hide_history),
        }
        with self._db.session() as session:
            self._sync_state.set_value(
                session,
                TMDB_PREVIEW_SEARCH_STATE_KEY,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )

    def load_explore_state(self) -> dict[str, Any]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, TMDB_PREVIEW_EXPLORE_STATE_KEY, "")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "imdb_min": str(payload.get("imdb_min") or ""),
            "tmdb_min": str(payload.get("tmdb_min") or ""),
            "hide_watchlisted": bool(payload.get("hide_watchlisted", False)),
            "hide_history": bool(payload.get("hide_history", False)),
            "hide_releases": bool(payload.get("hide_releases", False)),
        }

    def save_explore_state(
        self,
        *,
        imdb_min: str,
        tmdb_min: str,
        hide_watchlisted: bool,
        hide_history: bool,
        hide_releases: bool,
    ) -> None:
        payload = {
            "imdb_min": str(imdb_min or ""),
            "tmdb_min": str(tmdb_min or ""),
            "hide_watchlisted": bool(hide_watchlisted),
            "hide_history": bool(hide_history),
            "hide_releases": bool(hide_releases),
        }
        with self._db.session() as session:
            self._sync_state.set_value(
                session,
                TMDB_PREVIEW_EXPLORE_STATE_KEY,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )

    def explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int = 1,
        limit: int = 24,
    ) -> TmdbCatalogPage:
        self._ensure_local_state_projection()
        client = self._tmdb_factory(self._auth.config)
        if feed == "trending":
            payload = client.trending_catalog(title_type, page=page)
            metric_kind = "weekly trend"
        elif feed == "anticipated":
            payload = client.discover_catalog(title_type, page=page, upcoming=True)
            metric_kind = "popularity"
        else:
            payload = client.discover_catalog(title_type, page=page, upcoming=False)
            metric_kind = "popularity"
        result = self._page_from_payload(
            payload,
            title_type=title_type,
            page=page,
            limit=limit,
            metric_kind=metric_kind,
        )
        result.items.sort(key=lambda item: self._explore_sort_key(item, feed))
        self._hydrate_imdb_metadata(result.items)
        self._decorate_local_state(result.items)
        return result

    def filtered_explore_titles(
        self,
        title_type: str,
        feed: str,
        *,
        page: int,
        limit: int,
        imdb_min: float | None,
        tmdb_min: float | None,
        max_scan_pages: int,
        hide_watchlisted: bool = False,
        hide_history: bool = False,
        hide_releases: bool = False,
    ) -> TmdbCatalogPage:
        return self._filtered_catalog_page(
            lambda source_page: self.explore_titles(
                title_type,
                feed,
                page=source_page,
                limit=limit,
            ),
            page=page,
            limit=limit,
            max_scan_pages=max_scan_pages,
            imdb_min=imdb_min,
            tmdb_min=tmdb_min,
            hide_watchlisted=hide_watchlisted,
            hide_history=hide_history,
            hide_releases=hide_releases,
            sort_key=lambda item: self._explore_sort_key(item, feed),
        )

    @staticmethod
    def _explore_sort_key(item: TmdbCatalogItem, feed: str) -> tuple[bool, float]:
        if feed == "trending":
            rank = item.explore_rank
            return rank is None, float(rank or 0)
        popularity = item.popularity
        return popularity is None, -float(popularity or 0)

    def local_release_items(self) -> list[TmdbCatalogItem]:
        self._ensure_local_state_projection()
        result: dict[tuple[str, int], TmdbCatalogItem] = {}
        with self._db.session() as session:
            for row in self._repository.list_release_states(session):
                snapshot = self._repository.snapshot(session, row.title_type, row.tmdb_id)
                item = self._item_from_snapshot(snapshot, row=row)
                if item is not None:
                    result[item.identity_key] = item
            if not self._tmdb_local_mode() and self._legacy_release_tracking is not None:
                try:
                    legacy_items = self._legacy_release_tracking.local_items()
                except Exception:
                    legacy_items = []
                for legacy in legacy_items:
                    tmdb_id = int(getattr(legacy, "tmdb_id", 0) or 0)
                    if tmdb_id <= 0:
                        continue
                    item = self._item_from_legacy(legacy)
                    existing = result.get(item.identity_key)
                    if existing is not None:
                        existing.trakt_id = item.trakt_id
                        existing.slug = existing.slug or item.slug
                        existing.imdb_id = existing.imdb_id or item.imdb_id
                        existing.overview = existing.overview or item.overview
                        existing.poster_url = existing.poster_url or item.poster_url
                        existing.backdrop_url = existing.backdrop_url or item.backdrop_url
                        existing.imdb_rating = (
                            existing.imdb_rating
                            if existing.imdb_rating is not None
                            else item.imdb_rating
                        )
                        existing.imdb_votes = (
                            existing.imdb_votes
                            if existing.imdb_votes is not None
                            else item.imdb_votes
                        )
                        existing.local_only = False
                        self._repository.upsert_snapshot(session, existing.snapshot())
                        continue
                    result[item.identity_key] = item
                    self._repository.upsert_snapshot(session, item.snapshot())
                    release_row = self._repository.upsert_release_state(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        title=item.title,
                        release_at=item.released_at,
                        list_count=getattr(legacy, "explore_metric_count", None),
                    )
                    release_row.acknowledged_at = (
                        datetime.now(tz=UTC).replace(tzinfo=None)
                        if bool(getattr(legacy, "release_acknowledged", False))
                        else None
                    )
        self._decorate_local_state(result.values())
        items = list(result.values())
        self._hydrate_imdb_metadata(items, resolve_missing_ids=False)
        return items

    def local_watchlist_items(self) -> list[TmdbCatalogItem]:
        self._ensure_local_state_projection()
        with self._db.session() as session:
            items = [
                item
                for snapshot in self._repository.list_snapshots(session)
                if self._repository.effective_state(
                    session,
                    operation_type="watchlist",
                    title_type=snapshot.title_type,
                    tmdb_id=snapshot.tmdb_id,
                )
                and (item := self._item_from_snapshot(snapshot)) is not None
            ]
        self._decorate_local_state(items)
        self._hydrate_imdb_metadata(items, resolve_missing_ids=False)
        return items

    def refresh_release_items(self) -> list[TmdbCatalogItem]:
        items = self.local_release_items()
        client = self._tmdb_factory(self._auth.config)
        refreshed: list[TmdbCatalogItem] = []
        for item in items:
            try:
                payload = client.get_catalog_details(item.title_type, item.tmdb_id)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                updated = self._item_from_payload(payload, title_type=item.title_type)
                if updated is not None:
                    updated.trakt_id = item.trakt_id
                    updated.is_release_tracked = True
                    item = updated
                    with self._db.session() as session:
                        self._repository.upsert_snapshot(session, item.snapshot())
                        self._repository.upsert_release_state(
                            session,
                            title_type=item.title_type,
                            tmdb_id=item.tmdb_id,
                            title=item.title,
                            release_at=item.released_at,
                            list_count=item.explore_metric_count,
                        )
            refreshed.append(item)
        self._decorate_local_state(refreshed)
        return refreshed

    def watchlist_state(self, title_type: str, tmdb_id: int) -> bool:
        self._ensure_local_state_projection()
        with self._db.session() as session:
            return self._repository.effective_state(
                session,
                operation_type="watchlist",
                title_type=title_type,
                tmdb_id=tmdb_id,
            )

    def history_state(self, title_type: str, tmdb_id: int, season: int | None = None, episode: int | None = None) -> bool:
        self._ensure_local_state_projection()
        with self._db.session() as session:
            return self._repository.effective_state(
                session,
                operation_type="history",
                title_type=title_type,
                tmdb_id=tmdb_id,
                season=season,
                episode=episode,
            )

    def rating_state(
        self,
        title_type: str,
        tmdb_id: int,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> int | None:
        with self._db.session() as session:
            value = self._repository.effective_value(
                session,
                operation_type="rating",
                title_type=title_type,
                tmdb_id=tmdb_id,
                season=season,
                episode=episode,
            )
        rating = _optional_int(value)
        return rating if rating is not None and 1 <= rating <= 10 else None

    def local_show_episode_ratings(self, tmdb_id: int) -> dict[tuple[int, int], int]:
        self._ensure_local_state_projection()
        with self._db.session() as session:
            rows = self._repository.list_intents(
                session,
                operation_type="rating",
                title_type="show",
                tmdb_id=int(tmdb_id),
            )
        return {
            (int(row.season), int(row.episode)): rating
            for row in rows
            if row.season is not None
            and row.episode is not None
            and (rating := _optional_int(_json_value(row.desired_state_json, None))) is not None
            and 1 <= rating <= 10
        }

    def set_rating(
        self,
        item: TmdbCatalogItem,
        *,
        rating: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        normalized_rating = int(rating)
        if not 1 <= normalized_rating <= 10:
            raise ValueError("Rating must be between 1 and 10.")
        payload = item.snapshot()
        payload.update({"rating": normalized_rating, "season": season, "episode": episode})
        mapped = self._mapped_trakt_id_for_mutation(item)
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            row = self._repository.set_value_intent(
                session,
                operation_type="rating",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                season=season,
                episode=episode,
                desired=normalized_rating,
                payload=payload,
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {
            "rating": normalized_rating,
            "local_only": mapped is None,
            "trakt_id": mapped,
        }

    def set_progress_paused(self, tmdb_id: int, *, paused: bool) -> bool:
        return self._set_local_progress_flag("pause", tmdb_id, bool(paused))

    def set_progress_dropped(self, tmdb_id: int, *, dropped: bool) -> bool:
        return self._set_local_progress_flag("drop", tmdb_id, bool(dropped))

    def _set_local_progress_flag(self, operation_type: str, tmdb_id: int, desired: bool) -> bool:
        item = self.get_item("show", int(tmdb_id))
        if item.trakt_id and not self._tmdb_local_mode():
            raise RuntimeError("Mapped shows use the normal Progress state.")
        with self._db.session() as session:
            self._repository.set_intent(
                session,
                operation_type=operation_type,
                title_type="show",
                tmdb_id=int(tmdb_id),
                desired=desired,
                payload=item.snapshot(),
            )
        return desired

    def local_progress_items(
        self,
        *,
        view: str = "active",
        sort_mode: str = "episode_release",
        descending: bool = True,
        limit: int | None = 50,
    ) -> list[ProgressSnapshot]:
        self._ensure_local_state_projection()
        normalized_view = str(getattr(view, "value", view) or "active").strip().lower()
        with self._db.session() as session:
            items = [
                progress
                for snapshot in self._repository.list_snapshots(session, title_type="show")
                if snapshot.trakt_id is None
                and (progress := self._local_progress_snapshot(session, snapshot)) is not None
            ]
        if normalized_view == "dropped":
            items = [item for item in items if item.is_dropped]
        elif normalized_view == "paused":
            items = [item for item in items if item.is_paused and not item.is_dropped]
        else:
            items = [item for item in items if not item.is_paused and not item.is_dropped]
        items = self._sort_local_progress(items, sort_mode=sort_mode, descending=descending)
        if limit is not None:
            items = items[: max(0, int(limit))]
        return items

    def local_history_rows(
        self,
        *,
        title_type: str | None = None,
        title_filter: str | None = None,
        rated_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_local_state_projection()
        rows: list[dict[str, Any]] = []
        needle = str(title_filter or "").strip().casefold()
        with self._db.session() as session:
            history_intents = [
                intent
                for intent in self._repository.list_intents(
                    session,
                    operation_type="history",
                    title_type=title_type,
                )
                if bool(_json_value(intent.desired_state_json, False))
            ]
            if not history_intents:
                return []
            history_keys = {
                (str(intent.title_type), int(intent.tmdb_id))
                for intent in history_intents
            }
            snapshots = {
                (str(snapshot.title_type), int(snapshot.tmdb_id)): snapshot
                for snapshot in self._repository.list_snapshots(
                    session,
                    title_type=title_type,
                )
                if (str(snapshot.title_type), int(snapshot.tmdb_id)) in history_keys
            }
            rating_intents = self._repository.list_intents(
                session,
                operation_type="rating",
                title_type=title_type,
            )
            ratings_by_key = {
                (
                    str(intent.title_type),
                    int(intent.tmdb_id),
                    _optional_int(intent.season),
                    _optional_int(intent.episode),
                ): _optional_int(_json_value(intent.desired_state_json, None))
                for intent in rating_intents
            }
            ratings_by_title: dict[tuple[str, int], list[int]] = {}
            for intent in rating_intents:
                rating = _optional_int(_json_value(intent.desired_state_json, None))
                if rating is None or not 1 <= rating <= 10:
                    continue
                ratings_by_title.setdefault(
                    (str(intent.title_type), int(intent.tmdb_id)),
                    [],
                ).append(rating)
            intents_by_title: dict[tuple[str, int], list[Any]] = {}
            for intent in history_intents:
                intents_by_title.setdefault(
                    (str(intent.title_type), int(intent.tmdb_id)),
                    [],
                ).append(intent)

            for identity, intents in intents_by_title.items():
                snapshot = snapshots.get(identity)
                if snapshot is None:
                    continue
                item = self._item_from_snapshot(snapshot)
                if item is None or (needle and needle not in item.title.casefold()):
                    continue
                episode_rows = {
                    (int(value["season"]), int(value["episode"])): value
                    for value in self._cached_episode_rows(item)
                }
                ratings = ratings_by_title.get(identity, [])
                title_average = (sum(ratings) / len(ratings)) if ratings else None
                for intent in intents:
                    payload = _json_dict(intent.payload_json)
                    watched_at = _parse_datetime(payload.get("watched_at")) or _as_utc(intent.updated_at)
                    season = _optional_int(intent.season)
                    episode = _optional_int(intent.episode)
                    episode_row = episode_rows.get((season, episode)) if season is not None and episode is not None else None
                    display_rating = ratings_by_key.get(
                        (item.title_type, int(item.tmdb_id), season, episode)
                    )
                    if rated_only and not (
                        item.title_type == "show"
                        and season is not None
                        and episode is not None
                        and display_rating is not None
                    ):
                        continue
                    episode_tmdb_rating = _optional_float((episode_row or {}).get("tmdb_rating"))
                    episode_tmdb_votes = _optional_int((episode_row or {}).get("tmdb_votes"))
                    episode_imdb_rating = _optional_float((episode_row or {}).get("imdb_rating"))
                    episode_imdb_votes = _optional_int((episode_row or {}).get("imdb_votes"))
                    rows.append(
                        {
                            "provider": "tmdb",
                            "tmdb_id": int(item.tmdb_id),
                            "title_trakt_id": int(item.trakt_id or 0),
                            "title": item.title,
                            "title_slug": item.slug,
                            "poster_url": item.poster_url,
                            "title_poster_status": ENRICH_STATUS_READY if item.poster_url else ENRICH_STATUS_CHECKED_NO_DATA,
                            "title_poster_refreshed_at": None,
                            "backdrop_url": item.backdrop_url,
                            "title_backdrop_status": ENRICH_STATUS_READY if item.backdrop_url else ENRICH_STATUS_CHECKED_NO_DATA,
                            "title_backdrop_refreshed_at": None,
                            "title_tmdb_rating": item.tmdb_rating,
                            "title_tmdb_votes": item.tmdb_votes,
                            "title_trakt_rating": None,
                            "title_trakt_votes": None,
                            "title_imdb_rating": item.imdb_rating,
                            "title_imdb_votes": item.imdb_votes,
                            "title_ratings_status": item.ratings_status,
                            "title_ratings_refreshed_at": None,
                            "title_episode_avg_rating": title_average,
                            "title_episode_rated_count": len(ratings),
                            "title_year": item.year,
                            "type": item.title_type,
                            "action": "watched",
                            "watched_at": watched_at,
                            "watched_at_known": True,
                            "season": season,
                            "episode": episode,
                            "episode_tmdb_season": _optional_int((episode_row or {}).get("tmdb_season")) or season,
                            "episode_tmdb_episode": _optional_int((episode_row or {}).get("tmdb_episode")) or episode,
                            "episode_title": str((episode_row or {}).get("title") or ""),
                            "episode_still_url": str((episode_row or {}).get("still_url") or ""),
                            "episode_still_status": (
                                ENRICH_STATUS_READY
                                if (episode_row or {}).get("still_url")
                                else ENRICH_STATUS_CHECKED_NO_DATA
                            ),
                            "episode_still_refreshed_at": None,
                            "episode_tmdb_rating": episode_tmdb_rating,
                            "episode_tmdb_votes": episode_tmdb_votes,
                            "episode_tmdb_status": (
                                ENRICH_STATUS_READY
                                if episode_tmdb_rating is not None and episode_tmdb_votes is not None
                                else ENRICH_STATUS_CHECKED_NO_DATA
                            ),
                            "episode_tmdb_refreshed_at": None,
                            "episode_trakt_rating": None,
                            "episode_trakt_votes": None,
                            "episode_trakt_status": ENRICH_STATUS_CHECKED_NO_DATA,
                            "episode_trakt_refreshed_at": None,
                            "episode_imdb_rating": episode_imdb_rating,
                            "episode_imdb_votes": episode_imdb_votes,
                            "episode_imdb_season": season,
                            "episode_imdb_episode": episode,
                            "episode_imdb_status": (
                                ENRICH_STATUS_READY
                                if episode_imdb_rating is not None and episode_imdb_votes is not None
                                else ENRICH_STATUS_CHECKED_NO_DATA
                            ),
                            "event_rating": display_rating,
                            "title_rating": display_rating if item.title_type == "movie" else None,
                            "display_rating": display_rating,
                        }
                    )
        rows.sort(
            key=lambda row: row.get("watched_at") or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if offset:
            rows = rows[max(0, int(offset)) :]
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows

    def enrich_history_episode_ratings(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill TMDb episode ratings for mapped legacy History rows by TMDb coordinates."""
        client = self._tmdb_factory(self._auth.config)
        metadata_by_season: dict[tuple[int, int], dict[int, dict[str, Any]]] = {}
        enriched: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            tmdb_id = _optional_int(value.get("tmdb_id"))
            season = _optional_int(value.get("season"))
            episode = _optional_int(value.get("episode"))
            if tmdb_id is None or season is None or episode is None:
                enriched.append(value)
                continue
            key = (tmdb_id, season)
            if key not in metadata_by_season:
                metadata_by_season[key] = self._episode_metadata(
                    client,
                    tmdb_id,
                    season,
                    refresh_remote=True,
                )
            metadata = metadata_by_season[key].get(episode, {})
            value["episode_tmdb_rating"] = _optional_float(metadata.get("vote_average"))
            value["episode_tmdb_votes"] = _optional_int(metadata.get("vote_count"))
            value["episode_tmdb_status"] = (
                ENRICH_STATUS_READY
                if value["episode_tmdb_rating"] is not None and value["episode_tmdb_votes"] is not None
                else ENRICH_STATUS_CHECKED_NO_DATA
            )
            enriched.append(value)
        return enriched

    def local_history_title_summaries(
        self,
        *,
        title_type: str | None = None,
        title_filter: str | None = None,
        rated_only: bool = False,
        sort_by: str = "last_watched",
        sort_direction: str = "desc",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, int], dict[str, Any]] = {}
        for row in self.local_history_rows(
            title_type=title_type,
            title_filter=title_filter,
            rated_only=False,
        ):
            key = (str(row["type"]), int(row["tmdb_id"]))
            group = groups.get(key)
            if group is None:
                my_rating = row.get("title_episode_avg_rating") if row["type"] == "show" else row.get("display_rating")
                group = {
                    **row,
                    "title_key": f"{row['type']}:tmdb:{row['tmdb_id']}",
                    "my_rating": my_rating,
                    "last_watched_at": row.get("watched_at"),
                    "last_watched_at_known": True,
                    "watched_count": 0,
                    "latest_season": row.get("season"),
                    "latest_episode": row.get("episode"),
                }
                groups[key] = group
            group["watched_count"] += 1
        result = [group for group in groups.values() if not rated_only or group.get("my_rating") is not None]
        normalized_sort = sort_by if sort_by in {"rating", "last_watched", "release_year"} else "last_watched"

        def sort_value(group):
            if normalized_sort == "rating":
                return group.get("my_rating")
            if normalized_sort == "release_year":
                return group.get("title_year")
            watched_at = group.get("last_watched_at")
            return watched_at.timestamp() if isinstance(watched_at, datetime) else None

        known = [group for group in result if sort_value(group) is not None]
        unknown = [group for group in result if sort_value(group) is None]
        known.sort(key=sort_value, reverse=sort_direction != "asc")
        unknown.sort(key=lambda group: (str(group.get("title", "")).casefold(), int(group.get("tmdb_id") or 0)))
        result = [*known, *unknown]
        if offset:
            result = result[max(0, int(offset)) :]
        if limit is not None:
            result = result[: max(0, int(limit))]
        return result

    def local_history_titles(self, *, title_type: str | None = None) -> list[str]:
        with self._db.session() as session:
            history_keys = {
                (str(intent.title_type), int(intent.tmdb_id))
                for intent in self._repository.list_intents(
                    session,
                    operation_type="history",
                    title_type=title_type,
                )
                if bool(_json_value(intent.desired_state_json, False))
            }
            if not history_keys:
                return []
            titles = {
                str(snapshot.title or "")
                for snapshot in self._repository.list_snapshots(
                    session,
                    title_type=title_type,
                )
                if (str(snapshot.title_type), int(snapshot.tmdb_id)) in history_keys
                and str(snapshot.title or "")
            }
        return sorted(titles, key=str.casefold)

    def _local_progress_snapshot(self, session, snapshot) -> ProgressSnapshot | None:
        item = self._item_from_snapshot(snapshot)
        if item is None:
            return None
        stored_payload = _json_dict(snapshot.payload_json)
        legacy_progress = stored_payload.get("local_progress")
        if isinstance(legacy_progress, dict) and not bool(stored_payload.get("local_progress_dirty")):
            return self._local_progress_from_payload(session, item, legacy_progress)
        history_rows = self._repository.list_intents(
            session,
            operation_type="history",
            title_type="show",
            tmdb_id=item.tmdb_id,
        )
        watched_rows = [
            row
            for row in history_rows
            if row.season is not None
            and row.episode is not None
            and bool(_json_value(row.desired_state_json, False))
        ]
        if not watched_rows:
            return None
        watched_keys = {(int(row.season), int(row.episode)) for row in watched_rows}
        episodes = self._cached_episode_rows(item)
        if not episodes:
            return None
        now = datetime.now(tz=UTC)
        aired = [
            episode
            for episode in episodes
            if episode["first_aired"] is None or _as_utc(episode["first_aired"]) <= now
        ]
        completed = sum(
            1
            for episode in aired
            if (int(episode["season"]), int(episode["episode"])) in watched_keys
        )
        next_row = next(
            (
                episode
                for episode in episodes
                if (int(episode["season"]), int(episode["episode"])) not in watched_keys
            ),
            None,
        )
        if next_row is None:
            return None

        def watched_at(row) -> datetime:
            payload = _json_dict(row.payload_json)
            return _parse_datetime(payload.get("watched_at")) or datetime.min.replace(tzinfo=UTC)

        last_intent = max(watched_rows, key=watched_at)
        last_key = (int(last_intent.season), int(last_intent.episode))
        last_row = next(
            (
                episode
                for episode in episodes
                if (int(episode["season"]), int(episode["episode"])) == last_key
            ),
            None,
        )
        ratings = []
        for row in self._repository.list_intents(
            session,
            operation_type="rating",
            title_type="show",
            tmdb_id=item.tmdb_id,
        ):
            rating = _optional_int(_json_value(row.desired_state_json, None))
            if rating is not None and 1 <= rating <= 10:
                ratings.append(rating)
        return ProgressSnapshot(
            trakt_id=0,
            title=item.title,
            completed=completed,
            aired=len(aired),
            percent_completed=(completed / len(aired) * 100.0) if aired else 0.0,
            next_episode=self._local_progress_episode(next_row),
            last_episode=self._local_progress_episode(last_row),
            poster_url=item.poster_url,
            poster_status=ENRICH_STATUS_READY if item.poster_url else ENRICH_STATUS_CHECKED_NO_DATA,
            status=item.status,
            title_imdb_rating=item.imdb_rating,
            title_imdb_votes=item.imdb_votes,
            title_ratings_status=item.ratings_status,
            title_episode_avg_rating=(sum(ratings) / len(ratings)) if ratings else None,
            is_paused=self._repository.effective_state(
                session,
                operation_type="pause",
                title_type="show",
                tmdb_id=item.tmdb_id,
            ),
            is_dropped=self._repository.effective_state(
                session,
                operation_type="drop",
                title_type="show",
                tmdb_id=item.tmdb_id,
            ),
            last_watched_at=watched_at(last_intent),
            title_year=item.year,
            provider="tmdb",
            tmdb_id=item.tmdb_id,
            title_tmdb_rating=item.tmdb_rating,
            title_tmdb_votes=item.tmdb_votes,
        )

    def _local_progress_from_payload(
        self,
        session,
        item: TmdbCatalogItem,
        progress: dict[str, Any],
    ) -> ProgressSnapshot | None:
        next_raw = progress.get("next_episode")
        last_raw = progress.get("last_episode")
        if next_raw is None:
            return None

        def episode(value: Any) -> EpisodeSummary | None:
            if not isinstance(value, dict):
                return None
            season = _optional_int(value.get("season"))
            number = _optional_int(value.get("episode"))
            if season is None or number is None or season <= 0 or number <= 0:
                return None
            return self._local_progress_episode(
                {
                    **value,
                    "show_tmdb_id": int(item.tmdb_id),
                    "season": season,
                    "episode": number,
                }
            )

        ratings = [
            rating
            for row in self._repository.list_intents(
                session,
                operation_type="rating",
                title_type="show",
                tmdb_id=item.tmdb_id,
            )
            if (rating := _optional_int(_json_value(row.desired_state_json, None))) is not None
            and 1 <= rating <= 10
        ]
        next_episode = episode(next_raw)
        if next_raw is not None and next_episode is None:
            return None
        return ProgressSnapshot(
            trakt_id=0,
            title=item.title,
            completed=max(0, _optional_int(progress.get("completed")) or 0),
            aired=max(0, _optional_int(progress.get("aired")) or 0),
            percent_completed=float(progress.get("percent_completed") or 0.0),
            next_episode=next_episode,
            last_episode=episode(last_raw),
            poster_url=item.poster_url,
            poster_status=ENRICH_STATUS_READY if item.poster_url else ENRICH_STATUS_CHECKED_NO_DATA,
            status=item.status,
            title_imdb_rating=item.imdb_rating,
            title_imdb_votes=item.imdb_votes,
            title_ratings_status=item.ratings_status,
            title_episode_avg_rating=(sum(ratings) / len(ratings)) if ratings else None,
            is_paused=self._repository.effective_state(
                session,
                operation_type="pause",
                title_type="show",
                tmdb_id=item.tmdb_id,
            ),
            is_dropped=self._repository.effective_state(
                session,
                operation_type="drop",
                title_type="show",
                tmdb_id=item.tmdb_id,
            ),
            last_watched_at=_parse_datetime(progress.get("last_watched_at")),
            title_year=item.year,
            provider="tmdb",
            tmdb_id=item.tmdb_id,
            title_tmdb_rating=item.tmdb_rating,
            title_tmdb_votes=item.tmdb_votes,
        )

    @staticmethod
    def _cached_episode_rows(item: TmdbCatalogItem) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for season in item.seasons:
            if not isinstance(season, dict):
                continue
            season_number = _optional_int(season.get("season_number"))
            if season_number is None or season_number <= 0:
                continue
            for raw_episode in season.get("episodes", []):
                if not isinstance(raw_episode, dict):
                    continue
                episode_number = _optional_int(raw_episode.get("episode"))
                if episode_number is None or episode_number <= 0:
                    continue
                result.append(
                    {
                        **raw_episode,
                        "show_tmdb_id": int(item.tmdb_id),
                        "season": season_number,
                        "episode": episode_number,
                        "tmdb_season": _optional_int(raw_episode.get("tmdb_season")) or season_number,
                        "tmdb_episode": _optional_int(raw_episode.get("tmdb_episode")) or episode_number,
                        "first_aired": _parse_datetime(raw_episode.get("first_aired")),
                    }
                )
        return sorted(result, key=lambda value: (int(value["season"]), int(value["episode"])))

    @staticmethod
    def _local_progress_episode(row: dict[str, Any] | None) -> EpisodeSummary | None:
        if row is None:
            return None
        still_url = _tmdb_local_asset_url(row.get("still_url"))
        tmdb_rating = _optional_float(row.get("tmdb_rating"))
        tmdb_votes = _optional_int(row.get("tmdb_votes"))
        imdb_rating = _optional_float(row.get("imdb_rating"))
        imdb_votes = _optional_int(row.get("imdb_votes"))
        return EpisodeSummary(
            trakt_id=synthetic_episode_id(
                int(row.get("show_tmdb_id") or 0),
                int(row["season"]),
                int(row["episode"]),
            ),
            season=int(row["season"]),
            number=int(row["episode"]),
            title=str(row.get("title") or ""),
            still_url=still_url,
            still_status=ENRICH_STATUS_READY if still_url else ENRICH_STATUS_CHECKED_NO_DATA,
            trakt_details_status=ENRICH_STATUS_CHECKED_NO_DATA,
            imdb_rating=imdb_rating,
            imdb_votes=imdb_votes,
            imdb_status=(
                ENRICH_STATUS_READY
                if imdb_rating is not None and imdb_votes is not None
                else ENRICH_STATUS_CHECKED_NO_DATA
            ),
            tmdb_season=_optional_int(row.get("tmdb_season")) or int(row["season"]),
            tmdb_episode=_optional_int(row.get("tmdb_episode")) or int(row["episode"]),
            first_aired=_parse_datetime(row.get("first_aired")),
            overview=str(row.get("overview") or ""),
            tmdb_rating=tmdb_rating,
            tmdb_votes=tmdb_votes,
        )

    @staticmethod
    def _sort_local_progress(
        items: list[ProgressSnapshot],
        *,
        sort_mode: str,
        descending: bool,
    ) -> list[ProgressSnapshot]:
        normalized = str(getattr(sort_mode, "value", sort_mode) or "episode_release")
        if normalized == "last_watched":
            def value(item):
                return item.last_watched_at
        elif normalized == "release_year":
            def value(item):
                return item.title_year
        else:
            def value(item):
                return item.next_episode.first_aired if item.next_episode else None
        known = [item for item in items if value(item) is not None]
        unknown = [item for item in items if value(item) is None]
        known.sort(key=value, reverse=bool(descending))
        unknown.sort(key=lambda item: (item.title.casefold(), int(item.tmdb_id or 0)))
        return [*known, *unknown]

    def release_state(self, title_type: str, tmdb_id: int) -> bool:
        with self._db.session() as session:
            return self._repository.effective_state(
                session,
                operation_type="release",
                title_type=title_type,
                tmdb_id=tmdb_id,
            )

    def set_release_acknowledged(
        self,
        title_type: str,
        tmdb_id: int,
        *,
        acknowledged: bool,
        title: str = "",
        release_at: datetime | None = None,
    ) -> bool:
        with self._db.session() as session:
            if self._repository.release_state(session, str(title_type), int(tmdb_id)) is None and title:
                self._repository.upsert_release_state(
                    session,
                    title_type=str(title_type),
                    tmdb_id=int(tmdb_id),
                    title=title,
                    release_at=release_at,
                )
            return self._repository.set_release_acknowledged(
                session,
                str(title_type),
                int(tmdb_id),
                bool(acknowledged),
            )

    def poll_releases(self, *, send_native: bool = True, refresh_remote: bool = True) -> list[dict[str, Any]]:
        """Poll preview release state without touching Trakt.

        The timing policy intentionally mirrors ``ReleaseTrackingService``;
        only the durable state namespace differs.
        """
        if refresh_remote:
            self.refresh_release_items()
        config = self._auth.config
        if not bool(getattr(config, "notifications_enabled", True)):
            return []
        now = datetime.now(tz=UTC)
        repeat = timedelta(minutes=max(1, int(getattr(config, "notification_repeat_minutes", 5) or 5)))
        sent: list[dict[str, Any]] = []
        with self._db.session() as session:
            for row in self._repository.list_release_states(session):
                release_at = _as_utc(row.release_at)
                if release_at is None or release_at > now or row.acknowledged_at is not None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now < release_at + timedelta(minutes=max(0, delay_minutes)):
                    continue
                last_sent = _as_utc(row.last_sent_at)
                if last_sent is not None and now - last_sent < repeat:
                    continue
                body = "Movie is now available" if row.title_type == "movie" else "Show has premiered"
                if send_native:
                    # NotificationMessage is the same native-notification path
                    # used by the Trakt release tracker.
                    self._send_notification(row.title, body)
                self._repository.mark_release_sent(session, row.title_type, int(row.tmdb_id))
                sent.append({"show_title": row.title, "message": body, "source": "release"})
        return sent

    def released_count(self) -> int:
        now = datetime.now(tz=UTC)
        with self._db.session() as session:
            return sum(
                1
                for row in self._repository.list_release_states(session)
                if (release_at := _as_utc(row.release_at)) is not None and release_at <= now
            )

    def has_due_unacknowledged_release(self) -> bool:
        now = datetime.now(tz=UTC)
        config = self._auth.config
        with self._db.session() as session:
            for row in self._repository.list_release_states(session):
                release_at = _as_utc(row.release_at)
                if release_at is None or row.acknowledged_at is not None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now >= release_at + timedelta(minutes=max(0, delay_minutes)):
                    return True
        return False

    def matured_release_keys(self) -> set[tuple[str, int]]:
        now = datetime.now(tz=UTC)
        config = self._auth.config
        with self._db.session() as session:
            result: set[tuple[str, int]] = set()
            for row in self._repository.list_release_states(session):
                release_at = _as_utc(row.release_at)
                if release_at is None:
                    continue
                delay_minutes = (
                    int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
                    if row.title_type == "movie"
                    else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
                )
                if now >= release_at + timedelta(minutes=max(0, delay_minutes)):
                    result.add((str(row.title_type), int(row.tmdb_id)))
            return result

    def notified_release_keys(self) -> set[tuple[str, int]]:
        with self._db.session() as session:
            return {
                (str(row.title_type), int(row.tmdb_id))
                for row in self._repository.list_release_states(session)
                if row.last_sent_at is not None and row.acknowledged_at is None
            }

    def get_item(self, title_type: str, tmdb_id: int) -> TmdbCatalogItem:
        self._ensure_local_state_projection()
        tmdb_local_mode = self._tmdb_local_mode()
        mapping_changed = False
        with self._db.session() as session:
            snapshot = self._repository.snapshot(session, title_type, tmdb_id)
            if snapshot is not None:
                item = self._item_from_snapshot(snapshot)
                identity = self._repository.identity(session, title_type, tmdb_id)
                stored = self._titles.by_tmdb_id(session, title_type, tmdb_id)
                if tmdb_local_mode:
                    if item is not None:
                        item.imdb_id = str(
                            (identity.imdb_id if identity is not None else "")
                            or (stored.imdb_id if stored is not None else "")
                            or item.imdb_id
                            or ""
                        )
                else:
                    if stored is not None and (identity is None or not identity.trakt_id):
                        if item is not None:
                            item.slug = str(stored.slug or "")
                        identity = self._repository.set_identity(
                            session,
                            title_type=title_type,
                            tmdb_id=tmdb_id,
                            trakt_id=int(stored.trakt_id),
                            imdb_id=str(stored.imdb_id or getattr(identity, "imdb_id", "") or ""),
                        )
                    if item is not None and identity is not None:
                        item.trakt_id = int(identity.trakt_id) if identity.trakt_id else None
                        item.imdb_id = str(identity.imdb_id or item.imdb_id or "")
                    if item is not None and identity is not None and identity.trakt_id:
                        if not item.slug:
                            stored = self._titles.get_title(session, int(identity.trakt_id))
                            item.slug = str(stored.slug or "") if stored is not None else ""
                        mapping_changed = bool(
                            self._repository.attach_mapping(
                                session,
                                title_type=title_type,
                                tmdb_id=tmdb_id,
                                trakt_id=int(identity.trakt_id),
                            )
                        )
            else:
                item = None
        if snapshot is None:
            try:
                payload = self._tmdb_factory(self._auth.config).get_catalog_details(title_type, tmdb_id) or {}
            except Exception:
                payload = {}
            item = self._item_from_payload(payload, title_type=title_type) if payload else None
        if item is None:
            item = TmdbCatalogItem(title_type=title_type, tmdb_id=int(tmdb_id), title=f"TMDb {tmdb_id}")
        item = self._apply_mode_boundary(item)
        if mapping_changed and not tmdb_local_mode:
            self.reconcile_mapped_intents()
        self._hydrate_imdb_metadata([item], resolve_missing_ids=True)
        self._decorate_local_state([item])
        return item

    def set_watchlisted(self, item: TmdbCatalogItem, watchlisted: bool) -> dict[str, Any]:
        mapped = self._mapped_trakt_id_for_mutation(item)
        if mapped and self._legacy_catalog is not None:
            self._legacy_catalog.set_watchlisted(
                item.title_type,
                mapped,
                watchlisted=watchlisted,
                snapshot={
                    "title": item.title,
                    "released_at": item.released_at.isoformat() if item.released_at else "",
                    "list_count": item.explore_metric_count,
                },
            )
            return {"watchlisted": bool(watchlisted), "local_only": False, "trakt_id": mapped}
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            row = self._repository.set_intent(
                session,
                operation_type="watchlist",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                desired=watchlisted,
                payload=item.snapshot(),
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"watchlisted": bool(watchlisted), "local_only": mapped is None, "trakt_id": mapped}

    def set_release_tracked(self, item: TmdbCatalogItem, tracked: bool) -> dict[str, Any]:
        mapped = self._mapped_trakt_id_for_mutation(item)
        if mapped and self._legacy_release_tracking is not None:
            self._legacy_release_tracking.set_tracked(
                item.title_type,
                mapped,
                tracked=tracked,
                list_count=item.explore_metric_count,
                title=item.title,
                released_at=item.released_at,
            )
            with self._db.session() as session:
                if tracked:
                    self._repository.upsert_release_state(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        title=item.title,
                        release_at=item.released_at,
                        list_count=item.explore_metric_count,
                    )
                else:
                    state = self._repository.release_state(session, item.title_type, item.tmdb_id)
                    if state is not None:
                        session.delete(state)
            return {"tracked": bool(tracked), "local_only": False, "trakt_id": mapped}
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            if tracked:
                self._repository.upsert_release_state(
                    session,
                    title_type=item.title_type,
                    tmdb_id=item.tmdb_id,
                    title=item.title,
                    release_at=item.released_at,
                    list_count=item.explore_metric_count,
                )
            else:
                state = self._repository.release_state(session, item.title_type, item.tmdb_id)
                if state is not None:
                    session.delete(state)
            row = self._repository.set_intent(
                session,
                operation_type="release",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                desired=tracked,
                payload=item.snapshot(),
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"tracked": bool(tracked), "local_only": mapped is None, "trakt_id": mapped}

    def mark_watched(
        self,
        item: TmdbCatalogItem,
        *,
        watched_at: datetime | None,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        mapped = self._mapped_trakt_id_for_mutation(item)
        if mapped and self._legacy_catalog is not None:
            return {"local_only": False, "trakt_id": mapped, "mapped": True}
        if watched_at is None:
            watched_at = datetime.now(tz=UTC)
        payload = item.snapshot()
        payload.update({"watched_at": watched_at.astimezone(UTC).isoformat(), "season": season, "episode": episode})
        removed_from_release_tracking = False
        removed_from_watchlist = False
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, item.snapshot())
            row = self._repository.set_intent(
                session,
                operation_type="history",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                season=season,
                episode=episode,
                desired=True,
                payload=payload,
                mapped_trakt_id=mapped,
            )
            removed_from_release_tracking = self._repository.delete_release_state(
                session,
                item.title_type,
                item.tmdb_id,
            )
            release_intent = self._repository.intent(
                session,
                operation_type="release",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
            )
            if release_intent is not None:
                removed_from_release_tracking = True
                self._repository.set_intent(
                    session,
                    operation_type="release",
                    title_type=item.title_type,
                    tmdb_id=item.tmdb_id,
                    desired=False,
                    payload=item.snapshot(),
                    mapped_trakt_id=mapped,
                )
            if item.title_type == "show" and self._repository.effective_state(
                session,
                operation_type="watchlist",
                title_type="show",
                tmdb_id=item.tmdb_id,
            ):
                removed_from_watchlist = True
                self._repository.set_intent(
                    session,
                    operation_type="watchlist",
                    title_type="show",
                    tmdb_id=item.tmdb_id,
                    desired=False,
                    payload=item.snapshot(),
                    mapped_trakt_id=mapped,
                )
            self._mark_local_progress_dirty(session, item)
        self._wake_if_mapped(row)
        return {
            "local_only": mapped is None,
            "trakt_id": mapped,
            "mapped": False,
            "removed_from_release_tracking": removed_from_release_tracking,
            "removed_from_watchlist": removed_from_watchlist,
        }

    def _mark_local_progress_dirty(self, session, item: TmdbCatalogItem) -> None:
        if not self._tmdb_local_mode() or item.title_type != "show":
            return
        snapshot = self._repository.snapshot(session, item.title_type, item.tmdb_id)
        if snapshot is None:
            return
        payload = _json_dict(snapshot.payload_json)
        payload["local_progress_dirty"] = True
        payload["trakt_id"] = None
        self._repository.upsert_snapshot(session, payload)

    def mark_show_scope_watched(
        self,
        item: TmdbCatalogItem,
        *,
        watched_at: datetime | None,
        season: int | None = None,
    ) -> int:
        panel = self.load_watch_panel(item.tmdb_id, season=season)
        selected = [
            episode
            for episode in panel.get("episodes", [])
            if bool(episode.get("released", True)) and not bool(episode.get("watched"))
        ]
        if season is None:
            selected = []
            for raw_season in panel.get("seasons", []):
                season_number = _optional_int(raw_season.get("season_number"))
                if season_number is None or season_number < 0:
                    continue
                season_panel = self.load_watch_panel(item.tmdb_id, season=season_number)
                selected.extend(
                    episode
                    for episode in season_panel.get("episodes", [])
                    if bool(episode.get("released", True)) and not bool(episode.get("watched"))
                )
        if not selected:
            raise RuntimeError("No released episodes matched this action.")
        for episode in selected:
            self.mark_watched(
                item,
                watched_at=watched_at,
                season=int(episode["season"]),
                episode=int(episode["episode"]),
            )
        return len(selected)

    def unwatch(
        self,
        item: TmdbCatalogItem,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        mapped = self._mapped_trakt_id_for_mutation(item)
        if mapped and self._legacy_catalog is not None:
            return {"local_only": False, "trakt_id": mapped, "mapped": True}
        with self._db.session() as session:
            row = self._repository.set_intent(
                session,
                operation_type="history",
                title_type=item.title_type,
                tmdb_id=item.tmdb_id,
                season=season,
                episode=episode,
                desired=False,
                payload=item.snapshot(),
                mapped_trakt_id=mapped,
            )
        self._wake_if_mapped(row)
        return {"local_only": mapped is None, "trakt_id": mapped, "mapped": False}

    def unwatch_show_scope(self, item: TmdbCatalogItem, *, season: int | None = None) -> int:
        with self._db.session() as session:
            states = self._repository.history_episode_states(session, tmdb_id=item.tmdb_id)
        selected = [
            key
            for key, watched in states.items()
            if watched and (season is None or key[0] == int(season))
        ]
        for episode_season, episode_number in selected:
            self.unwatch(item, season=episode_season, episode=episode_number)
        return len(selected)

    def load_watch_panel(
        self,
        tmdb_id: int,
        *,
        season: int | None = None,
        _fallback_to_legacy: bool = False,
    ) -> dict[str, Any]:
        mapped_item = self.get_item("show", tmdb_id)
        if (
            not self._tmdb_local_mode()
            and mapped_item.trakt_id
            and self._legacy_search_watch is not None
            and (
                normalize_catalog_provider_mode(
                    getattr(self._auth.config, "catalog_provider_mode", "trakt")
                ) == "trakt"
                or _fallback_to_legacy
            )
        ):
            panel = self._legacy_search_watch.load_show_panel(
                int(mapped_item.trakt_id),
                default_season=season,
                season_layout="trakt",
            )
            selected_season = season
            if selected_season is None:
                selected_season = next(
                    (
                        int(value.season)
                        for value in panel.seasons
                        if bool(getattr(value, "is_default", False))
                    ),
                    None,
                )
            if selected_season is None and panel.seasons:
                selected_season = int(panel.seasons[0].season)
            selected = next(
                (value for value in panel.seasons if int(value.season) == int(selected_season or 0)),
                None,
            )
            if selected is None and _fallback_to_legacy and panel.seasons:
                selected = next(
                    (value for value in panel.seasons if bool(getattr(value, "is_default", False))),
                    panel.seasons[0],
                )
                selected_season = int(selected.season)
            episodes = []
            if selected is not None:
                client = self._tmdb_factory(self._auth.config)
                tmdb_metadata_by_season = {
                    physical_season: self._episode_metadata(
                        client,
                        int(tmdb_id),
                        physical_season,
                        refresh_remote=True,
                    )
                    for physical_season in {int(value.season) for value in selected.episodes}
                }
                episodes = [
                    {
                        "season": int(value.season),
                        "episode": int(value.number),
                        "title": str(value.title or ""),
                        "overview": "",
                        "first_aired": (
                            _replace_calendar_date(value.first_aired, tmdb_air_date)
                            if (tmdb_air_date := _parse_datetime(tmdb_metadata.get("air_date"))) is not None
                            else value.first_aired
                        ),
                        "still_url": str(value.still_url or ""),
                        "tmdb_rating": _optional_float(tmdb_metadata.get("vote_average")),
                        "tmdb_votes": _optional_int(tmdb_metadata.get("vote_count")),
                        "imdb_rating": getattr(value, "imdb_rating", None),
                        "imdb_votes": getattr(value, "imdb_votes", None),
                        "imdb_season": getattr(value, "imdb_season", None),
                        "imdb_episode": getattr(value, "imdb_episode", None),
                        "user_rating": getattr(value, "user_rating", None),
                        "released": bool(value.is_released),
                        "watched": bool(value.is_watched),
                    }
                    for value in selected.episodes
                    for tmdb_metadata in [
                        tmdb_metadata_by_season.get(int(value.season), {}).get(int(value.number), {})
                    ]
                ]
            selected_episodes = episodes
            watched_count = int(panel.watched_count)
            released_count = int(panel.released_count)
            released_watched_count = int(panel.released_watched_count)
            selected_watched_count = sum(1 for value in selected_episodes if value["watched"])
            selected_released_count = sum(1 for value in selected_episodes if value.get("released", True))
            selected_released_watched_count = sum(
                1 for value in selected_episodes if value.get("released", True) and value["watched"]
            )
            return {
                "tmdb_id": int(tmdb_id),
                "trakt_id": int(mapped_item.trakt_id),
                "title": str(panel.title or mapped_item.title),
                "overview": mapped_item.overview,
                "seasons": [
                    {
                        "season_number": int(value.season),
                        "episode_count": len(value.episodes),
                    }
                    for value in panel.seasons
                ],
                "selected_season": selected_season,
                "episodes": episodes,
                **self._watch_panel_header(
                    mapped_item,
                    watched_count=watched_count,
                    released_count=released_count,
                    released_watched_count=released_watched_count,
                    selected_watched_count=selected_watched_count,
                    selected_released_count=selected_released_count,
                    selected_released_watched_count=selected_released_watched_count,
                ),
            }
        client = self._tmdb_factory(self._auth.config)
        cached_seasons = [
            dict(value)
            for value in mapped_item.seasons
            if isinstance(value, dict)
        ]
        try:
            details = client.get_catalog_details("show", tmdb_id) or {}
        except Exception:
            if self._tmdb_local_mode():
                cached_panel = self._load_cached_tmdb_watch_panel(mapped_item, season=season)
                if cached_panel is not None:
                    return cached_panel
            if not self._tmdb_local_mode() and mapped_item.trakt_id and self._legacy_search_watch is not None:
                return self.load_watch_panel(
                    tmdb_id,
                    season=season,
                    _fallback_to_legacy=True,
                )
            raise
        current_tmdb_rating = _optional_float(details.get("vote_average"))
        current_tmdb_votes = _optional_int(details.get("vote_count"))
        if current_tmdb_rating is not None:
            mapped_item.tmdb_rating = current_tmdb_rating
        if current_tmdb_votes is not None:
            mapped_item.tmdb_votes = current_tmdb_votes
        seasons = [
            item
            for item in details.get("seasons", [])
            if isinstance(item, dict) and int(item.get("season_number") or -1) >= 0
        ]
        if self._tmdb_local_mode() and self._prefer_cached_tmdb_seasons(seasons, cached_seasons):
            cached_panel = self._load_cached_tmdb_watch_panel(mapped_item, season=season)
            if cached_panel is not None:
                return cached_panel
        selected = season
        if selected is None:
            selected = next((int(item.get("season_number")) for item in seasons if int(item.get("episode_count") or 0) > 0), 1)
        try:
            payload = client.get_catalog_season(tmdb_id, selected) or {}
        except Exception:
            if self._tmdb_local_mode():
                cached_panel = self._load_cached_tmdb_watch_panel(mapped_item, season=selected)
                if cached_panel is not None:
                    return cached_panel
            if not self._tmdb_local_mode() and mapped_item.trakt_id and self._legacy_search_watch is not None:
                return self.load_watch_panel(
                    tmdb_id,
                    season=selected,
                    _fallback_to_legacy=True,
                )
            raise
        if self._tmdb_local_mode() and not payload.get("episodes"):
            cached_panel = self._load_cached_tmdb_watch_panel(mapped_item, season=selected)
            if cached_panel is not None:
                return cached_panel
        episodes: list[dict[str, Any]] = []
        with self._db.session() as session:
            for raw in payload.get("episodes", []) if isinstance(payload, dict) else []:
                if not isinstance(raw, dict):
                    continue
                number = int(raw.get("episode_number") or 0)
                if number <= 0:
                    continue
                episodes.append(
                    {
                        "season": selected,
                        "episode": number,
                        "tmdb_season": selected,
                        "tmdb_episode": number,
                        "title": str(raw.get("name") or ""),
                        "overview": str(raw.get("overview") or ""),
                        "first_aired": _parse_datetime(raw.get("air_date")),
                        "still_url": self._still_url(raw.get("still_path")),
                        "tmdb_rating": _optional_float(raw.get("vote_average")),
                        "tmdb_votes": _optional_int(raw.get("vote_count")),
                        "released": bool(
                            (air_date := _parse_datetime(raw.get("air_date"))) is None
                            or air_date <= datetime.now(tz=UTC)
                        ),
                        "watched": self._repository.effective_state(
                            session,
                            operation_type="history",
                            title_type="show",
                            tmdb_id=tmdb_id,
                            season=selected,
                            episode=number,
                        ),
                        "user_rating": _optional_int(
                            self._repository.effective_value(
                                session,
                                operation_type="rating",
                                title_type="show",
                                tmdb_id=tmdb_id,
                                season=selected,
                                episode=number,
                            )
                        ),
                    }
                )
            all_states = self._repository.history_episode_states(session, tmdb_id=tmdb_id)
        watched_count = sum(1 for watched in all_states.values() if watched)
        selected_watched_count = sum(1 for value in episodes if value["watched"])
        released_count = sum(1 for value in episodes if value["released"])
        released_watched_count = sum(1 for value in episodes if value["released"] and value["watched"])
        mapped_item.title = str(details.get("name") or mapped_item.title or "")
        mapped_item.overview = str(details.get("overview") or mapped_item.overview or "")
        mapped_item.seasons = self._merge_cached_season(
            mapped_item.seasons,
            seasons,
            selected,
            episodes,
        )
        with self._db.session() as session:
            self._repository.upsert_snapshot(session, mapped_item.snapshot())
        return {
            "tmdb_id": int(tmdb_id),
            "title": str(details.get("name") or mapped_item.title or ""),
            "overview": str(details.get("overview") or mapped_item.overview or ""),
            "seasons": seasons,
            "selected_season": selected,
            "episodes": episodes,
            **self._watch_panel_header(
                mapped_item,
                watched_count=watched_count,
                released_count=released_count,
                released_watched_count=min(watched_count, released_count),
                selected_watched_count=selected_watched_count,
                selected_released_count=released_count,
                selected_released_watched_count=released_watched_count,
            ),
        }

    @staticmethod
    def _prefer_cached_tmdb_seasons(
        remote_seasons: list[dict[str, Any]],
        cached_seasons: list[dict[str, Any]],
    ) -> bool:
        remote_numbers = {
            number
            for raw in remote_seasons
            if (number := _optional_int(raw.get("season_number"))) is not None and number >= 0
        }
        cached_numbers = {
            number
            for raw in cached_seasons
            if (number := _optional_int(raw.get("season_number"))) is not None and number > 0
            and isinstance(raw.get("episodes"), list)
            and raw.get("episodes")
        }
        return bool(cached_numbers and (cached_numbers - remote_numbers))

    def _load_cached_tmdb_watch_panel(
        self,
        item: TmdbCatalogItem,
        *,
        season: int | None,
    ) -> dict[str, Any] | None:
        cached_seasons = [
            value
            for value in item.seasons
            if isinstance(value, dict)
            and (_optional_int(value.get("season_number")) or 0) > 0
            and isinstance(value.get("episodes"), list)
            and value.get("episodes")
        ]
        if not cached_seasons:
            return None
        season_numbers = [int(value["season_number"]) for value in cached_seasons]
        selected = int(season) if season is not None and int(season) in season_numbers else season_numbers[0]
        episodes = [
            row
            for row in self._cached_episode_rows(item)
            if int(row["season"]) == selected
        ]
        if not episodes:
            return None
        now = datetime.now(tz=UTC)
        with self._db.session() as session:
            all_states = self._repository.history_episode_states(session, tmdb_id=item.tmdb_id)
            rendered: list[dict[str, Any]] = []
            for row in episodes:
                first_aired = row.get("first_aired")
                released = first_aired is None or _as_utc(first_aired) <= now
                key = (int(row["season"]), int(row["episode"]))
                rendered.append(
                    {
                        "season": key[0],
                        "episode": key[1],
                        "tmdb_season": _optional_int(row.get("tmdb_season")) or key[0],
                        "tmdb_episode": _optional_int(row.get("tmdb_episode")) or key[1],
                        "title": str(row.get("title") or ""),
                        "overview": str(row.get("overview") or ""),
                        "first_aired": first_aired,
                        "still_url": _tmdb_local_asset_url(row.get("still_url")),
                        "tmdb_rating": _optional_float(row.get("tmdb_rating")),
                        "tmdb_votes": _optional_int(row.get("tmdb_votes")),
                        "imdb_rating": _optional_float(row.get("imdb_rating")),
                        "imdb_votes": _optional_int(row.get("imdb_votes")),
                        "released": released,
                        "watched": bool(all_states.get(key, False)),
                        "user_rating": _optional_int(
                            self._repository.effective_value(
                                session,
                                operation_type="rating",
                                title_type="show",
                                tmdb_id=item.tmdb_id,
                                season=key[0],
                                episode=key[1],
                            )
                        ),
                    }
                )
        all_episodes = self._cached_episode_rows(item)
        released_count = sum(
            1
            for row in all_episodes
            if row.get("first_aired") is None or _as_utc(row["first_aired"]) <= now
        )
        watched_count = sum(1 for watched in all_states.values() if watched)
        selected_watched_count = sum(1 for row in rendered if row["watched"])
        selected_released_count = sum(1 for row in rendered if row["released"])
        selected_released_watched_count = sum(1 for row in rendered if row["released"] and row["watched"])
        return {
            "tmdb_id": int(item.tmdb_id),
            "title": item.title,
            "overview": item.overview,
            "seasons": [
                {
                    "season_number": int(value["season_number"]),
                    "episode_count": len(value.get("episodes", [])),
                }
                for value in sorted(cached_seasons, key=lambda value: int(value["season_number"]))
            ],
            "selected_season": selected,
            "episodes": rendered,
            **self._watch_panel_header(
                item,
                watched_count=watched_count,
                released_count=released_count,
                released_watched_count=min(watched_count, released_count),
                selected_watched_count=selected_watched_count,
                selected_released_count=selected_released_count,
                selected_released_watched_count=selected_released_watched_count,
            ),
        }

    @staticmethod
    def _merge_cached_season(
        cached: list[dict[str, Any]],
        season_summaries: list[dict[str, Any]],
        selected_season: int,
        episodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[int, dict[str, Any]] = {}
        for raw in [*cached, *season_summaries]:
            if not isinstance(raw, dict):
                continue
            number = _optional_int(raw.get("season_number"))
            if number is None or number < 0:
                continue
            existing = merged.setdefault(number, {"season_number": number})
            existing.update({key: value for key, value in raw.items() if key != "episodes"})
            if isinstance(raw.get("episodes"), list):
                existing["episodes"] = list(raw["episodes"])
        selected = merged.setdefault(int(selected_season), {"season_number": int(selected_season)})
        selected["episode_count"] = max(
            int(selected.get("episode_count") or 0),
            len(episodes),
        )
        selected["episodes"] = [
            {
                "episode": int(value["episode"]),
                "tmdb_season": _optional_int(value.get("tmdb_season")) or int(selected_season),
                "tmdb_episode": _optional_int(value.get("tmdb_episode")) or int(value["episode"]),
                "title": str(value.get("title") or ""),
                "overview": str(value.get("overview") or ""),
                "first_aired": (
                    value["first_aired"].isoformat()
                    if isinstance(value.get("first_aired"), datetime)
                    else str(value.get("first_aired") or "")
                ),
                "still_url": str(value.get("still_url") or ""),
                "tmdb_rating": _optional_float(value.get("tmdb_rating")),
                "tmdb_votes": _optional_int(value.get("tmdb_votes")),
                "imdb_rating": _optional_float(value.get("imdb_rating")),
                "imdb_votes": _optional_int(value.get("imdb_votes")),
            }
            for value in episodes
        ]
        return [merged[number] for number in sorted(merged)]

    @staticmethod
    def _watch_panel_header(
        item: TmdbCatalogItem,
        *,
        watched_count: int,
        released_count: int,
        released_watched_count: int,
        selected_watched_count: int,
        selected_released_count: int,
        selected_released_watched_count: int,
    ) -> dict[str, Any]:
        return {
            "tmdb_rating": item.tmdb_rating,
            "tmdb_votes": item.tmdb_votes,
            "imdb_rating": item.imdb_rating,
            "imdb_votes": item.imdb_votes,
            "ratings_status": item.ratings_status,
            "watched_count": watched_count,
            "released_count": released_count,
            "released_watched_count": released_watched_count,
            "can_mark_title": released_count > released_watched_count,
            "can_unwatch_title": watched_count > 0,
            "can_mark_season": selected_released_count > selected_released_watched_count,
            "can_unwatch_season": selected_watched_count > 0,
        }

    def reconcile_mapped_intents(self) -> int:
        """Move local preview intents with a known Trakt id into the normal outbox."""
        if self._trakt_outbox is None or normalize_catalog_provider_mode(
            getattr(self._auth.config, "catalog_provider_mode", "trakt")
        ) != "trakt":
            return 0
        moved = 0
        with self._db.session() as session:
            rows = self._repository.list_intents(session)
            for row in rows:
                if row.mapped_trakt_id is None or row.status == "exported":
                    continue
                payload = _json_dict(row.payload_json)
                desired = bool(_json_value(row.desired_state_json, False))
                operation = str(row.operation_type)
                key: str | None = None
                if operation in {"watchlist", "release"}:
                    base_member = self._mapped_membership_base(
                        operation,
                        row.title_type,
                        int(row.mapped_trakt_id),
                    )
                    key = self._trakt_outbox.enqueue_membership(
                        session,
                        operation_type="watchlist" if operation == "watchlist" else "release",
                        title_type=row.title_type,
                        trakt_id=int(row.mapped_trakt_id),
                        base_member=base_member,
                        desired_member=desired,
                        snapshot=payload,
                        origin="tmdb_preview",
                        metadata={
                            "tmdb_preview_intent_id": int(row.id),
                            "tmdb_preview_revision": int(row.revision or 0),
                        },
                    )
                elif operation == "history":
                    watched_at = _parse_datetime(payload.get("watched_at"))
                    base_watched = self._mapped_history_base(row.title_type, int(row.mapped_trakt_id))
                    if desired and watched_at is None:
                        watched_at = datetime.now(tz=UTC)
                    if desired or base_watched:
                        key = self._trakt_outbox.enqueue_history(
                            session,
                            title_type=row.title_type,
                            trakt_id=int(row.mapped_trakt_id),
                            title=str(payload.get("title") or ""),
                            desired_watched=True,
                            base_watched=base_watched,
                            watched_at=watched_at,
                            season=_optional_int(payload.get("season")),
                            episode=_optional_int(payload.get("episode")),
                            origin="tmdb_preview",
                            metadata={
                                "tmdb_preview_intent_id": int(row.id),
                                "tmdb_preview_revision": int(row.revision or 0),
                            },
                        )
                if key:
                    row.status = "exported"
                    moved += 1
                elif row.status == "mapped_pending":
                    # The desired state already equals the known Trakt base;
                    # there is no POST to await, so this revision is complete.
                    row.status = "exported"
        if moved:
            self._trakt_outbox.wake()
        return moved

    def _mapped_membership_base(self, operation: str, title_type: str, trakt_id: int) -> bool:
        try:
            if operation == "watchlist" and self._legacy_catalog is not None:
                return (str(title_type), int(trakt_id)) in self._legacy_catalog.watchlist_keys()
            if operation == "release" and self._legacy_release_tracking is not None:
                return (str(title_type), int(trakt_id)) in self._legacy_release_tracking.local_keys()
        except Exception:
            pass
        return False

    def _mapped_history_base(self, title_type: str, trakt_id: int) -> bool:
        try:
            if self._legacy_catalog is not None:
                return (str(title_type), int(trakt_id)) in self._legacy_catalog.history_keys()
        except Exception:
            pass
        return False

    def on_trakt_outbox_delivered(self, operation) -> None:
        """Keep the local revision after its delivery to an external tracker."""
        payload = getattr(operation, "payload", {})
        if not isinstance(payload, dict) or str(getattr(operation, "origin", "")) != "tmdb_preview":
            return
        intent_id = _optional_int(payload.get("tmdb_preview_intent_id"))
        revision = _optional_int(payload.get("tmdb_preview_revision"))
        if intent_id is None or revision is None:
            return
        with self._db.session() as session:
            self._repository.mark_intent_exported(session, intent_id, revision)

    def clear(self) -> None:
        with self._db.session() as session:
            self._repository.clear(session)
        self.clear_cache()

    def clear_cache(self) -> None:
        with self._db.session() as session:
            self._sync_state.set_value(session, TMDB_PREVIEW_SEARCH_STATE_KEY, "")
            self._sync_state.set_value(session, TMDB_PREVIEW_EXPLORE_STATE_KEY, "")
        client = self._tmdb_factory(self._auth.config)
        clear_cache = getattr(client, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()

    def _send_notification(self, title: str, body: str) -> None:
        # Importing the concrete sender lazily keeps the catalog service easy to
        # exercise with a lightweight auth/client double in unit tests.
        sender = getattr(self, "_notification_sender", None)
        if sender is not None:
            sender.send(NotificationMessage(title=title, body=body))

    def set_notification_sender(self, sender) -> None:
        self._notification_sender = sender

    def _filtered_catalog_page(
        self,
        fetch_page: Callable[[int], TmdbCatalogPage],
        *,
        page: int,
        limit: int,
        max_scan_pages: int,
        imdb_min: float | None,
        tmdb_min: float | None,
        hide_watchlisted: bool,
        hide_history: bool,
        hide_releases: bool = False,
        sort_key: Callable[[TmdbCatalogItem], tuple[bool, float]] | None = None,
    ) -> TmdbCatalogPage:
        current_page = max(1, int(page))
        page_size = max(1, int(limit))
        target_count = current_page * page_size + 1
        matches: list[TmdbCatalogItem] = []
        source_page_number = 1
        exhausted = False
        while (
            len(matches) < target_count
            and not exhausted
            and source_page_number <= max(1, int(max_scan_pages))
        ):
            source_page = fetch_page(source_page_number)
            matches.extend(
                item
                for item in source_page.items
                if (
                    (imdb_min is None or (item.imdb_rating is not None and item.imdb_rating >= imdb_min))
                    and (tmdb_min is None or (item.tmdb_rating is not None and item.tmdb_rating >= tmdb_min))
                    and (not hide_watchlisted or not item.is_watchlisted)
                    and (not hide_history or not item.is_in_history)
                    and (not hide_releases or not item.is_release_tracked)
                )
            )
            exhausted = source_page_number >= source_page.page_count
            source_page_number += 1
        if sort_key is not None:
            matches.sort(key=sort_key)
        start = (current_page - 1) * page_size
        end = start + page_size
        has_next = len(matches) > end
        return TmdbCatalogPage(
            items=matches[start:end],
            page=current_page,
            page_count=current_page + 1 if has_next else current_page,
        )

    def _page_from_payload(
        self,
        payload: dict[str, Any],
        *,
        title_type: str | None,
        page: int,
        limit: int,
        metric_kind: str,
    ) -> TmdbCatalogPage:
        raw_items = payload.get("results", []) if isinstance(payload, dict) else []
        items: list[TmdbCatalogItem] = []
        payload_page = _optional_int(payload.get("page")) or max(1, int(page))
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            item_type = self._item_type(raw, title_type)
            if item_type is None:
                continue
            item = self._item_from_payload(
                raw,
                title_type=item_type,
                metric_kind=metric_kind,
                explore_rank=((payload_page - 1) * 20 + index + 1) if metric_kind == "weekly trend" else None,
            )
            if item is not None:
                items.append(item)
        total_pages = _optional_int(payload.get("total_pages")) or max(1, int(page))
        return TmdbCatalogPage(items=items[: max(1, int(limit))], page=max(1, int(page)), page_count=max(1, total_pages))

    def _item_from_payload(
        self,
        payload: dict[str, Any],
        *,
        title_type: str,
        metric_kind: str = "",
        explore_rank: int | None = None,
    ) -> TmdbCatalogItem | None:
        tmdb_id = _optional_int(payload.get("id"))
        if tmdb_id is None or tmdb_id <= 0:
            return None
        released_at = _parse_datetime(payload.get("release_date") or payload.get("first_air_date"))
        title = str(payload.get("title") or payload.get("name") or "").strip()
        external_ids = payload.get("external_ids", {})
        item = TmdbCatalogItem(
            title_type="show" if title_type == "show" else "movie",
            tmdb_id=tmdb_id,
            title=title,
            year=_year(released_at),
            overview=str(payload.get("overview") or ""),
            status=str(payload.get("status") or ""),
            poster_url=self._poster_url(payload.get("poster_path")),
            backdrop_url=self._backdrop_url(payload.get("backdrop_path")),
            tmdb_rating=_optional_float(payload.get("vote_average")),
            tmdb_votes=_optional_int(payload.get("vote_count")),
            imdb_id=str(external_ids.get("imdb_id") or "") if isinstance(external_ids, dict) else "",
            popularity=_optional_float(payload.get("popularity")),
            released_at=released_at,
            explore_metric_kind=metric_kind,
            explore_rank=explore_rank,
            is_future_release=bool(released_at and released_at > datetime.now(tz=UTC)),
            is_notification_matured=self._release_is_matured(released_at, title_type),
        )
        tmdb_local_mode = self._tmdb_local_mode()
        mapping_changed = False
        with self._db.session() as session:
            identity = self._repository.identity(session, item.title_type, item.tmdb_id)
            stored = self._titles.by_tmdb_id(session, item.title_type, item.tmdb_id)
            if tmdb_local_mode:
                item.imdb_id = str(
                    (identity.imdb_id if identity is not None else "")
                    or (stored.imdb_id if stored is not None else "")
                    or item.imdb_id
                    or ""
                )
            else:
                if stored is not None and (identity is None or not identity.trakt_id):
                    item.slug = str(stored.slug or "")
                    identity = self._repository.set_identity(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        trakt_id=int(stored.trakt_id),
                        imdb_id=str(stored.imdb_id or item.imdb_id or getattr(identity, "imdb_id", "") or ""),
                    )
                elif item.imdb_id and (identity is None or not identity.imdb_id):
                    identity = self._repository.set_identity(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        trakt_id=int(identity.trakt_id) if identity is not None and identity.trakt_id else None,
                        imdb_id=item.imdb_id,
                    )
                if identity is not None:
                    item.trakt_id = int(identity.trakt_id) if identity.trakt_id else None
                    item.imdb_id = str(identity.imdb_id or item.imdb_id or "")
                    if item.trakt_id:
                        if not item.slug:
                            stored = self._titles.get_title(session, int(item.trakt_id))
                            item.slug = str(stored.slug or "") if stored is not None else ""
                        if stored is not None:
                            item.imdb_rating = stored.imdb_rating
                            item.imdb_votes = stored.imdb_votes
                            item.ratings_status = str(stored.ratings_status or ENRICH_STATUS_UNKNOWN)
                        mapping_changed = bool(self._repository.attach_mapping(
                            session,
                            title_type=item.title_type,
                            tmdb_id=item.tmdb_id,
                            trakt_id=item.trakt_id,
                        ))
            item = self._apply_mode_boundary(item)
            self._repository.upsert_snapshot(session, item.snapshot())
            item.is_watchlisted = self._repository.effective_state(
                session, operation_type="watchlist", title_type=item.title_type, tmdb_id=item.tmdb_id
            )
            item.is_in_history = self._repository.effective_state(
                session, operation_type="history", title_type=item.title_type, tmdb_id=item.tmdb_id
            )
            item.is_release_tracked = self._repository.effective_state(
                session, operation_type="release", title_type=item.title_type, tmdb_id=item.tmdb_id
            ) or self._repository.release_state(session, item.title_type, item.tmdb_id) is not None
            release_row = self._repository.release_state(session, item.title_type, item.tmdb_id)
            item.release_acknowledged = bool(release_row and release_row.acknowledged_at)
        item = self._apply_mode_boundary(item)
        if mapping_changed and not tmdb_local_mode:
            self.reconcile_mapped_intents()
        return item

    def _item_from_snapshot(self, snapshot, *, row=None) -> TmdbCatalogItem | None:
        if snapshot is None:
            return None
        stored_payload = _json_dict(snapshot.payload_json)
        item = TmdbCatalogItem(
            title_type=str(snapshot.title_type),
            tmdb_id=int(snapshot.tmdb_id),
            trakt_id=int(snapshot.trakt_id) if snapshot.trakt_id else None,
            imdb_id=str(snapshot.imdb_id or ""),
            title=str(snapshot.title or ""),
            slug=str(stored_payload.get("slug") or ""),
            year=snapshot.year,
            overview=str(snapshot.overview or ""),
            status=str(snapshot.status or ""),
            poster_url=(
                _tmdb_local_asset_url(snapshot.poster_url)
                if self._tmdb_local_mode()
                else str(snapshot.poster_url or "")
            ),
            backdrop_url=(
                _tmdb_local_asset_url(snapshot.backdrop_url)
                if self._tmdb_local_mode()
                else str(snapshot.backdrop_url or "")
            ),
            tmdb_rating=snapshot.tmdb_rating,
            tmdb_votes=snapshot.tmdb_votes,
            imdb_rating=_optional_float(stored_payload.get("imdb_rating")),
            imdb_votes=_optional_int(stored_payload.get("imdb_votes")),
            ratings_status=str(stored_payload.get("ratings_status") or ENRICH_STATUS_UNKNOWN),
            popularity=snapshot.popularity,
            released_at=_as_utc(snapshot.released_at),
            is_future_release=bool(snapshot.released_at and _as_utc(snapshot.released_at) > datetime.now(tz=UTC)),
            is_notification_matured=self._release_is_matured(snapshot.released_at, snapshot.title_type),
            is_release_tracked=row is not None,
            release_acknowledged=bool(row and row.acknowledged_at),
            seasons=[
                _sanitize_snapshot_season(value) if self._tmdb_local_mode() else dict(value)
                for value in stored_payload.get("seasons", [])
                if isinstance(value, dict)
            ],
        )
        return self._apply_mode_boundary(item)

    def _item_from_legacy(self, legacy) -> TmdbCatalogItem:
        released_at = _as_utc(getattr(legacy, "released_at", None))
        return TmdbCatalogItem(
            title_type=str(legacy.title_type),
            tmdb_id=int(legacy.tmdb_id),
            trakt_id=int(legacy.trakt_id),
            imdb_id=str(getattr(legacy, "imdb_id", "") or ""),
            title=str(legacy.title or ""),
            slug=str(getattr(legacy, "slug", "") or ""),
            year=getattr(legacy, "year", None),
            overview=str(getattr(legacy, "overview", "") or ""),
            poster_url=str(getattr(legacy, "poster_url", "") or ""),
            backdrop_url=str(getattr(legacy, "backdrop_url", "") or ""),
            tmdb_rating=getattr(legacy, "tmdb_rating", None),
            tmdb_votes=getattr(legacy, "tmdb_votes", None),
            imdb_rating=getattr(legacy, "imdb_rating", None),
            imdb_votes=getattr(legacy, "imdb_votes", None),
            ratings_status=str(getattr(legacy, "ratings_status", ENRICH_STATUS_UNKNOWN) or ENRICH_STATUS_UNKNOWN),
            explore_metric_kind="lists",
            explore_metric_count=getattr(legacy, "explore_metric_count", None),
            released_at=released_at,
            is_release_tracked=True,
            release_acknowledged=bool(getattr(legacy, "release_acknowledged", False)),
            is_future_release=bool(released_at and released_at > datetime.now(tz=UTC)),
            is_notification_matured=self._release_is_matured(released_at, legacy.title_type),
            local_only=False,
        )

    def _hydrate_imdb_metadata(
        self,
        items: list[TmdbCatalogItem],
        *,
        resolve_missing_ids: bool = True,
    ) -> None:
        if not items:
            return
        unresolved: list[TmdbCatalogItem] = []
        with self._db.session() as session:
            for item in items:
                identity = self._repository.identity(session, item.title_type, item.tmdb_id)
                if identity is not None and identity.imdb_id:
                    item.imdb_id = str(identity.imdb_id)
                elif identity is not None and str(identity.status or "") == ENRICH_STATUS_CHECKED_NO_DATA:
                    item.ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
                elif (
                    resolve_missing_ids
                    and not item.imdb_id
                    and (identity is None or str(identity.status or "") != ENRICH_STATUS_CHECKED_NO_DATA)
                ):
                    unresolved.append(item)

        if unresolved:
            client = self._tmdb_factory(self._auth.config)

            def load_details(item: TmdbCatalogItem) -> tuple[TmdbCatalogItem, dict[str, Any] | None]:
                try:
                    payload = client.get_catalog_details(item.title_type, item.tmdb_id)
                except Exception:
                    payload = None
                return item, payload if isinstance(payload, dict) else None

            workers = min(6, len(unresolved))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tmdb-imdb-id") as pool:
                resolved = list(pool.map(load_details, unresolved))
            with self._db.session() as session:
                for item, payload in resolved:
                    external_ids = payload.get("external_ids", {}) if payload else {}
                    imdb_id = str(external_ids.get("imdb_id") or "") if isinstance(external_ids, dict) else ""
                    if imdb_id:
                        item.imdb_id = imdb_id
                    elif payload is not None:
                        item.ratings_status = ENRICH_STATUS_CHECKED_NO_DATA
                    identity = self._repository.identity(session, item.title_type, item.tmdb_id)
                    self._repository.set_identity(
                        session,
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                        trakt_id=(
                            int(identity.trakt_id)
                            if identity is not None and identity.trakt_id
                            else (int(item.trakt_id) if item.trakt_id else None)
                        ),
                        imdb_id=item.imdb_id,
                        status=(
                            ENRICH_STATUS_READY
                            if item.imdb_id
                            else (ENRICH_STATUS_CHECKED_NO_DATA if payload is not None else ENRICH_STATUS_UNKNOWN)
                        ),
                    )

        imdb_ready = bool(self._imdb_client is not None and self._imdb_client.is_ready())
        for item in items:
            if item.imdb_rating is not None and item.imdb_votes is not None:
                item.ratings_status = ENRICH_STATUS_READY
                continue
            if imdb_ready and item.imdb_id:
                summary = TitleSummary(
                    trakt_id=int(item.trakt_id or 0),
                    title_type=item.title_type,
                    title=item.title,
                    imdb_id=item.imdb_id,
                )
                summary = self._imdb_client.enrich_title(summary)
                item.imdb_rating = summary.imdb_rating
                item.imdb_votes = summary.imdb_votes
                item.ratings_status = (
                    ENRICH_STATUS_READY
                    if item.imdb_rating is not None and item.imdb_votes is not None
                    else ENRICH_STATUS_CHECKED_NO_DATA
                )
        with self._db.session() as session:
            for item in items:
                self._repository.upsert_snapshot(session, item.snapshot())

    def _decorate_local_state(self, items) -> None:
        with self._db.session() as session:
            for item in items:
                item.is_watchlisted = item.is_watchlisted or self._repository.effective_state(
                    session, operation_type="watchlist", title_type=item.title_type, tmdb_id=item.tmdb_id
                )
                item.is_in_history = item.is_in_history or self._repository.effective_state(
                    session, operation_type="history", title_type=item.title_type, tmdb_id=item.tmdb_id
                )
                history_rows = self._repository.list_intents(
                    session,
                    operation_type="history",
                    title_type=item.title_type,
                    tmdb_id=item.tmdb_id,
                )
                item.is_in_history = item.is_in_history or any(
                    bool(_json_value(row.desired_state_json, False))
                    for row in history_rows
                )
                item.is_release_tracked = item.is_release_tracked or self._repository.effective_state(
                    session, operation_type="release", title_type=item.title_type, tmdb_id=item.tmdb_id
                )
                ratings = [
                    rating
                    for row in self._repository.list_intents(
                        session,
                        operation_type="rating",
                        title_type=item.title_type,
                        tmdb_id=item.tmdb_id,
                    )
                    if (rating := _optional_int(_json_value(row.desired_state_json, None))) is not None
                    and 1 <= rating <= 10
                ]
                if ratings:
                    item.title_episode_avg_rating = sum(ratings) / len(ratings)

    def _wake_if_mapped(self, row) -> None:
        if self._tmdb_local_mode():
            return
        if row is not None and row.mapped_trakt_id is not None and self._trakt_outbox is not None:
            self.reconcile_mapped_intents()

    def _release_is_matured(self, release_at: datetime | None, title_type: str) -> bool:
        release_at = _as_utc(release_at)
        if release_at is None:
            return False
        config = self._auth.config
        delay_minutes = (
            int(getattr(config, "movie_release_notification_delay_minutes", 10080) or 0)
            if title_type == "movie"
            else int(getattr(config, "notification_release_delay_minutes", 120) or 0)
        )
        return datetime.now(tz=UTC) >= release_at + timedelta(minutes=max(0, delay_minutes))

    @staticmethod
    def _item_type(payload: dict[str, Any], requested: str | None) -> str | None:
        if requested in {"movie", "show"}:
            return requested
        media_type = str(payload.get("media_type") or "")
        if media_type == "movie" or "title" in payload:
            return "movie"
        if media_type == "tv" or "name" in payload:
            return "show"
        return None

    @staticmethod
    def _poster_url(path: Any) -> str:
        return f"{TMDB_POSTER_IMAGE_BASE}{path}" if isinstance(path, str) and path else ""

    @staticmethod
    def _backdrop_url(path: Any) -> str:
        return f"{TMDB_BACKDROP_IMAGE_BASE}{path}" if isinstance(path, str) and path else ""

    @staticmethod
    def _still_url(path: Any) -> str:
        return f"https://image.tmdb.org/t/p/w780{path}" if isinstance(path, str) and path else ""


def _snapshot_seasons(seasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        serialized: dict[str, Any] = {}
        for key, value in season.items():
            if key == "episodes" and isinstance(value, list):
                serialized[key] = [
                    {
                        episode_key: (
                            episode_value.isoformat()
                            if isinstance(episode_value, datetime)
                            else episode_value
                        )
                        for episode_key, episode_value in episode.items()
                    }
                    for episode in value
                    if isinstance(episode, dict)
                ]
            else:
                serialized[key] = value.isoformat() if isinstance(value, datetime) else value
        result.append(serialized)
    return result


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _tmdb_local_asset_url(value: Any) -> str:
    raw = str(value or "").strip()
    if "media.trakt.tv" in raw.casefold():
        return ""
    return raw


def _sanitize_snapshot_season(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    episodes = result.get("episodes")
    if isinstance(episodes, list):
        result["episodes"] = [
            {
                **episode,
                **(
                    {"still_url": _tmdb_local_asset_url(episode.get("still_url"))}
                    if isinstance(episode, dict) and "still_url" in episode
                    else {}
                ),
            }
            for episode in episodes
            if isinstance(episode, dict)
        ]
    return result


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _replace_calendar_date(current: datetime | None, calendar_date: datetime) -> datetime:
    if current is None:
        return datetime.combine(calendar_date.date(), datetime.min.time(), tzinfo=UTC)
    current_utc = _as_utc(current)
    return current_utc.replace(
        year=calendar_date.year,
        month=calendar_date.month,
        day=calendar_date.day,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _legacy_datetime_text(value: datetime | None) -> str:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else ""


def _year(value: datetime | None) -> int | None:
    return value.year if value is not None else None


def _json_value(raw: str, fallback: Any) -> Any:
    import json

    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _json_dict(raw: str) -> dict[str, Any]:
    value = _json_value(raw, {})
    return value if isinstance(value, dict) else {}
