from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import unicodedata
from typing import cast

from sqlalchemy import and_, delete, desc, exists, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
)
from trakt_tracker.domain import (
    CalendarEntry,
    EpisodeSummary,
    ProgressSnapshot,
    ProgressSortMode,
    ProgressView,
    TitleSummary,
)

from .models import (
    EpisodeCache,
    HistoryEvent,
    NotificationLog,
    ReleaseTrackingState,
    SyncState,
    Title,
    TitleAlias,
    TitleAliasRefresh,
    UserTitleState,
    WatchProgress,
)


def normalize_title_search(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True, slots=True)
class TitleAliasTarget:
    title_type: str
    trakt_id: int
    title: str
    status: str
    last_checked_at: datetime | None


def _is_show_title_fallback(title: str, trakt_id: int) -> bool:
    return (title or "").strip() == f"Show {trakt_id}"


class TitleRepository:
    _UNSET = object()

    def upsert_title(self, session: Session, title: TitleSummary) -> Title:
        model = session.scalar(select(Title).where(Title.trakt_id == title.trakt_id))
        if model is None:
            model = Title(trakt_id=title.trakt_id, title_type=title.title_type, title=title.title)
            session.add(model)
        model.title = title.title
        model.title_type = title.title_type
        model.year = title.year
        if title.overview:
            model.overview = title.overview
        if title.poster_url:
            model.poster_url = title.poster_url
        if title.backdrop_url:
            model.backdrop_url = title.backdrop_url
        if title.status:
            model.status = title.status
        if title.slug:
            model.slug = title.slug
        if title.poster_url:
            model.poster_url = title.poster_url
            model.poster_status = ENRICH_STATUS_READY
            model.poster_refreshed_at = (
                title.poster_refreshed_at.replace(tzinfo=None)
                if title.poster_refreshed_at is not None and title.poster_refreshed_at.tzinfo is not None
                else title.poster_refreshed_at
            ) or model.poster_refreshed_at or datetime.now(tz=UTC).replace(tzinfo=None)
        if title.backdrop_url:
            model.backdrop_url = title.backdrop_url
            model.backdrop_status = ENRICH_STATUS_READY
            model.backdrop_refreshed_at = (
                title.backdrop_refreshed_at.replace(tzinfo=None)
                if title.backdrop_refreshed_at is not None and title.backdrop_refreshed_at.tzinfo is not None
                else title.backdrop_refreshed_at
            ) or model.backdrop_refreshed_at or datetime.now(tz=UTC).replace(tzinfo=None)
        if title.trakt_rating is not None:
            model.trakt_rating = title.trakt_rating
        if title.trakt_votes is not None:
            model.trakt_votes = title.trakt_votes
        if title.tmdb_id is not None:
            model.tmdb_id = title.tmdb_id
        if title.tmdb_rating is not None:
            model.tmdb_rating = title.tmdb_rating
        if title.tmdb_votes is not None:
            model.tmdb_votes = title.tmdb_votes
        if title.imdb_id:
            model.imdb_id = title.imdb_id
        if title.imdb_rating is not None:
            model.imdb_rating = title.imdb_rating
        if title.imdb_votes is not None:
            model.imdb_votes = title.imdb_votes
        if model.poster_url and not model.poster_status:
            model.poster_status = ENRICH_STATUS_READY
        if model.backdrop_url and not model.backdrop_status:
            model.backdrop_status = ENRICH_STATUS_READY
        if title.poster_refreshed_at is not None:
            model.poster_refreshed_at = (
                title.poster_refreshed_at.replace(tzinfo=None)
                if title.poster_refreshed_at.tzinfo is not None
                else title.poster_refreshed_at
            )
        if title.backdrop_refreshed_at is not None:
            model.backdrop_refreshed_at = (
                title.backdrop_refreshed_at.replace(tzinfo=None)
                if title.backdrop_refreshed_at.tzinfo is not None
                else title.backdrop_refreshed_at
            )
        if (
            model.ratings_status == ENRICH_STATUS_UNKNOWN
            and model.trakt_rating is not None
            and model.trakt_votes is not None
        ):
            model.ratings_status = ENRICH_STATUS_READY
        session.flush()
        return model

    def get_title(self, session: Session, trakt_id: int) -> Title | None:
        return session.scalar(select(Title).where(Title.trakt_id == trakt_id))

    def by_trakt_ids(self, session: Session, trakt_ids: list[int]) -> dict[int, Title]:
        unique_ids = list(dict.fromkeys(int(trakt_id) for trakt_id in trakt_ids))
        if not unique_ids:
            return {}
        return {int(row.trakt_id): row for row in session.scalars(select(Title).where(Title.trakt_id.in_(unique_ids)))}

    def update_poster_enrich_state(
        self,
        session: Session,
        trakt_id: int,
        *,
        status: str,
        poster_url: str | object = _UNSET,
    ) -> Title | None:
        model = self.get_title(session, trakt_id)
        if model is None:
            return None
        if poster_url is not self._UNSET:
            incoming_poster_url = str(poster_url or "")
            if incoming_poster_url or not model.poster_url:
                model.poster_url = incoming_poster_url
        if model.poster_url:
            model.poster_status = ENRICH_STATUS_READY
        else:
            model.poster_status = status
        model.poster_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        session.flush()
        return model

    def update_backdrop_enrich_state(
        self,
        session: Session,
        trakt_id: int,
        *,
        status: str,
        backdrop_url: str | object = _UNSET,
    ) -> Title | None:
        model = self.get_title(session, trakt_id)
        if model is None:
            return None
        if backdrop_url is not self._UNSET:
            incoming_backdrop_url = str(backdrop_url or "")
            if incoming_backdrop_url or not model.backdrop_url:
                model.backdrop_url = incoming_backdrop_url
        if model.backdrop_url:
            model.backdrop_status = ENRICH_STATUS_READY
        else:
            model.backdrop_status = status
        model.backdrop_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        session.flush()
        return model

    def update_ratings_enrich_state(
        self,
        session: Session,
        trakt_id: int,
        *,
        status: str,
        trakt_rating: float | None | object = _UNSET,
        trakt_votes: int | None | object = _UNSET,
        tmdb_id: int | None | object = _UNSET,
        tmdb_rating: float | None | object = _UNSET,
        tmdb_votes: int | None | object = _UNSET,
        imdb_id: str | object = _UNSET,
        imdb_rating: float | None | object = _UNSET,
        imdb_votes: int | None | object = _UNSET,
    ) -> Title | None:
        model = self.get_title(session, trakt_id)
        if model is None:
            return None
        if trakt_rating is not self._UNSET:
            model.trakt_rating = cast(float | None, trakt_rating)
        if trakt_votes is not self._UNSET:
            model.trakt_votes = cast(int | None, trakt_votes)
        if tmdb_id is not self._UNSET:
            model.tmdb_id = cast(int | None, tmdb_id)
        if tmdb_rating is not self._UNSET:
            model.tmdb_rating = cast(float | None, tmdb_rating)
        if tmdb_votes is not self._UNSET:
            model.tmdb_votes = cast(int | None, tmdb_votes)
        if imdb_id is not self._UNSET:
            model.imdb_id = str(imdb_id or "")
        if imdb_rating is not self._UNSET:
            model.imdb_rating = cast(float | None, imdb_rating)
        if imdb_votes is not self._UNSET:
            model.imdb_votes = cast(int | None, imdb_votes)
        if model.trakt_rating is not None and model.trakt_votes is not None:
            model.ratings_status = ENRICH_STATUS_READY
        else:
            model.ratings_status = status
        model.ratings_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        session.flush()
        return model

    def list_titles(self, session: Session) -> list[Title]:
        return list(session.scalars(select(Title).order_by(Title.title)))

    def list_artwork_batch(self, session: Session, *, after_id: int, limit: int) -> list[Title]:
        stmt = (
            select(Title)
            .where(Title.id > max(0, int(after_id)))
            .where(Title.poster_url.is_not(None), Title.poster_url != "")
            .order_by(Title.id)
            .limit(max(1, int(limit)))
        )
        return list(session.scalars(stmt))

    def list_title_targets(
        self,
        session: Session,
        *,
        statuses: tuple[str, ...] | None = None,
        include_missing_url: bool = False,
    ) -> list[tuple[int, str]]:
        stmt = select(Title)
        if statuses:
            stmt = stmt.where(Title.poster_status.in_(statuses))
        if include_missing_url:
            stmt = stmt.where(or_(Title.poster_url == "", Title.poster_url.is_(None)))
        rows = list(session.scalars(stmt))
        return [(int(row.trakt_id), str(row.title_type)) for row in rows]


class UserStateRepository:
    def ensure_state(self, session: Session, title_id: int) -> UserTitleState:
        state = session.scalar(select(UserTitleState).where(UserTitleState.title_id == title_id))
        if state is None:
            state = UserTitleState(title_id=title_id)
            session.add(state)
            session.flush()
        return state

    def ratings_by_trakt_ids(self, session: Session, trakt_ids: list[int]) -> dict[int, int]:
        if not trakt_ids:
            return {}
        stmt = (
            select(Title.trakt_id, UserTitleState.rating)
            .join(UserTitleState, UserTitleState.title_id == Title.id)
            .where(Title.trakt_id.in_(trakt_ids))
            .where(UserTitleState.rating.is_not(None))
        )
        return {trakt_id: rating for trakt_id, rating in session.execute(stmt) if rating is not None}

    def clear_ratings(self, session: Session) -> None:
        session.execute(update(UserTitleState).values(rating=None))
        session.flush()

    def set_archived(self, session: Session, trakt_id: int, archived: bool) -> None:
        title = self._progress_title(session, trakt_id)
        if title is None:
            return
        state = self.ensure_state(session, title.id)
        state.archived = archived
        if archived:
            state.tracked = False
        else:
            state.tracked = True
        session.flush()

    def set_paused(self, session: Session, trakt_id: int, paused: bool) -> None:
        title = self._progress_title(session, trakt_id)
        if title is None:
            return
        state = self.ensure_state(session, title.id)
        state.paused = paused
        if paused:
            state.tracked = True
        session.flush()

    def progress_state(self, session: Session, trakt_id: int) -> UserTitleState | None:
        return session.scalar(
            select(UserTitleState)
            .join(Title, Title.id == UserTitleState.title_id)
            .where(Title.trakt_id == trakt_id)
        )

    @staticmethod
    def _progress_title(session: Session, trakt_id: int) -> Title | None:
        title = session.scalar(select(Title).where(Title.trakt_id == trakt_id))
        if title is not None:
            return title
        progress = session.scalar(select(WatchProgress).where(WatchProgress.show_trakt_id == trakt_id))
        if progress is None:
            return None
        title = Title(
            trakt_id=trakt_id,
            title_type="show",
            title=str(progress.show_title or ""),
        )
        session.add(title)
        session.flush()
        return title

    def sync_progress_archived_states(self, session: Session, dropped_ids: set[int]) -> None:
        paused_ids = {
            int(trakt_id)
            for trakt_id in session.scalars(
                select(Title.trakt_id)
                .join(UserTitleState, UserTitleState.title_id == Title.id)
                .where(UserTitleState.paused.is_(True))
            )
        }
        self.sync_progress_hidden_states(session, dropped_ids=dropped_ids, paused_ids=paused_ids)

    def sync_progress_hidden_states(
        self,
        session: Session,
        *,
        dropped_ids: set[int],
        paused_ids: set[int],
    ) -> None:
        show_ids = list(session.scalars(select(WatchProgress.show_trakt_id).distinct()))
        for show_trakt_id in show_ids:
            title = self._progress_title(session, int(show_trakt_id))
            if title is None:
                continue
            state = self.ensure_state(session, title.id)
            is_dropped = title.trakt_id in dropped_ids
            state.archived = is_dropped
            state.paused = title.trakt_id in paused_ids
            state.tracked = not is_dropped
        session.flush()


class TitleAliasRepository:
    def list_history_targets(self, session: Session, *, language: str) -> list[TitleAliasTarget]:
        normalized_language = language.strip().casefold()
        history_titles = (
            select(
                HistoryEvent.title_type.label("title_type"),
                HistoryEvent.title_trakt_id.label("title_trakt_id"),
                func.max(HistoryEvent.title).label("title"),
                func.max(HistoryEvent.watched_at).label("last_event_at"),
            )
            .group_by(HistoryEvent.title_type, HistoryEvent.title_trakt_id)
            .subquery()
        )
        rows = session.execute(
            select(
                history_titles.c.title_type,
                history_titles.c.title_trakt_id,
                history_titles.c.title,
                TitleAliasRefresh.status,
                TitleAliasRefresh.last_checked_at,
            )
            .select_from(
                history_titles.outerjoin(
                    TitleAliasRefresh,
                    and_(
                        TitleAliasRefresh.title_type == history_titles.c.title_type,
                        TitleAliasRefresh.title_trakt_id == history_titles.c.title_trakt_id,
                        TitleAliasRefresh.language == normalized_language,
                    ),
                )
            )
            .order_by(desc(history_titles.c.last_event_at))
        )
        return [
            TitleAliasTarget(
                title_type=str(title_type),
                trakt_id=int(trakt_id),
                title=str(title or ""),
                status=str(status or ENRICH_STATUS_UNKNOWN),
                last_checked_at=last_checked_at,
            )
            for title_type, trakt_id, title, status, last_checked_at in rows
        ]

    def replace_trakt_aliases(
        self,
        session: Session,
        *,
        title_type: str,
        trakt_id: int,
        language: str,
        aliases: list[str],
        checked_at: datetime,
    ) -> None:
        normalized_language = language.strip().casefold()
        session.execute(
            delete(TitleAlias)
            .where(TitleAlias.title_type == title_type)
            .where(TitleAlias.title_trakt_id == trakt_id)
            .where(TitleAlias.language == normalized_language)
            .where(TitleAlias.source == "trakt")
        )
        unique_aliases: dict[str, str] = {}
        for alias in aliases:
            normalized = normalize_title_search(alias)
            if normalized:
                unique_aliases.setdefault(normalized, alias.strip())
        for normalized, alias in unique_aliases.items():
            session.add(
                TitleAlias(
                    title_type=title_type,
                    title_trakt_id=trakt_id,
                    language=normalized_language,
                    title=alias,
                    normalized_title=normalized,
                    source="trakt",
                )
            )
        refresh = self._ensure_refresh(session, title_type, trakt_id, normalized_language)
        refresh.status = ENRICH_STATUS_READY if unique_aliases else ENRICH_STATUS_CHECKED_NO_DATA
        refresh.last_checked_at = checked_at
        refresh.error_count = 0

    def mark_retryable_failure(
        self,
        session: Session,
        *,
        title_type: str,
        trakt_id: int,
        language: str,
        checked_at: datetime,
    ) -> None:
        normalized_language = language.strip().casefold()
        refresh = self._ensure_refresh(session, title_type, trakt_id, normalized_language)
        refresh.status = ENRICH_STATUS_RETRYABLE_FAILURE
        refresh.last_checked_at = checked_at
        refresh.error_count = int(refresh.error_count or 0) + 1

    @staticmethod
    def _ensure_refresh(
        session: Session,
        title_type: str,
        trakt_id: int,
        language: str,
    ) -> TitleAliasRefresh:
        refresh = session.scalar(
            select(TitleAliasRefresh)
            .where(TitleAliasRefresh.title_type == title_type)
            .where(TitleAliasRefresh.title_trakt_id == trakt_id)
            .where(TitleAliasRefresh.language == language)
        )
        if refresh is None:
            refresh = TitleAliasRefresh(
                title_type=title_type,
                title_trakt_id=trakt_id,
                language=language,
            )
            session.add(refresh)
        return refresh


class HistoryRepository:
    _LOCAL_WATCH_DEDUP_WINDOW = timedelta(minutes=15)

    def find_recent_local_watch(
        self,
        session: Session,
        *,
        title_trakt_id: int,
        season: int | None,
        episode: int | None,
        watched_at: datetime,
        watched_at_known: bool = True,
    ) -> HistoryEvent | None:
        existing_local = session.scalar(
            select(HistoryEvent)
            .where(HistoryEvent.source == "local")
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_trakt_id == title_trakt_id)
            .where(HistoryEvent.season == season)
            .where(HistoryEvent.episode == episode)
            .where(HistoryEvent.watched_at_known == watched_at_known)
            .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at))
            .limit(1)
        )
        if existing_local is not None and not watched_at_known:
            return existing_local
        if (
            existing_local is not None
            and self._within_local_watch_window(watched_at, existing_local.watched_at)
        ):
            return existing_local
        return None

    def add_event(
        self,
        session: Session,
        *,
        trakt_history_id: int | None,
        title_trakt_id: int,
        title: str,
        title_type: str,
        action: str,
        watched_at: datetime,
        watched_at_known: bool = True,
        season: int | None = None,
        episode: int | None = None,
        rating: int | None = None,
        source: str = "local",
    ) -> HistoryEvent:
        if trakt_history_id is not None:
            existing = session.scalar(select(HistoryEvent).where(HistoryEvent.trakt_history_id == trakt_history_id))
            if existing is not None:
                existing.title_trakt_id = title_trakt_id
                existing.title = title
                existing.title_type = title_type
                existing.action = action
                existing.watched_at = watched_at
                existing.watched_at_known = watched_at_known
                existing.season = season
                existing.episode = episode
                existing.rating = rating
                existing.source = source
                session.flush()
                if action == "watched":
                    return self._delete_other_watched_duplicates(session, existing)
                return existing
        if trakt_history_id is None and source == "local" and action == "watched":
            existing_local = self.find_recent_local_watch(
                session,
                title_trakt_id=title_trakt_id,
                season=season,
                episode=episode,
                watched_at=watched_at,
                watched_at_known=watched_at_known,
            )
            if existing_local is not None:
                existing_local.title = title
                existing_local.title_type = title_type
                existing_local.watched_at = watched_at
                existing_local.watched_at_known = watched_at_known
                existing_local.rating = rating
                session.flush()
                return self._delete_other_watched_duplicates(session, existing_local)
        event = HistoryEvent(
            trakt_history_id=trakt_history_id,
            title_trakt_id=title_trakt_id,
            title=title,
            title_type=title_type,
            action=action,
            watched_at=watched_at,
            watched_at_known=watched_at_known,
            season=season,
            episode=episode,
            rating=rating,
            source=source,
        )
        session.add(event)
        session.flush()
        if action == "watched":
            return self._delete_other_watched_duplicates(session, event)
        return event

    def _delete_other_watched_duplicates(self, session: Session, keep_event: HistoryEvent) -> HistoryEvent:
        candidates = session.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == keep_event.title_type)
            .where(HistoryEvent.title_trakt_id == keep_event.title_trakt_id)
            .where(HistoryEvent.season == keep_event.season)
            .where(HistoryEvent.episode == keep_event.episode)
            .order_by(
                desc(HistoryEvent.watched_at_known),
                desc(HistoryEvent.watched_at),
                desc(HistoryEvent.trakt_history_id),
                desc(HistoryEvent.id),
            )
        ).all()
        canonical = candidates[0]
        for duplicate in candidates[1:]:
            session.delete(duplicate)
        session.flush()
        return canonical

    def collapse_duplicate_watches(self, session: Session) -> None:
        rows = session.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
        ).all()
        seen: set[tuple[str, int, int | None, int | None]] = set()
        for row in rows:
            key = (row.title_type, row.title_trakt_id, row.season, row.episode)
            if key in seen:
                session.delete(row)
                continue
            seen.add(key)

    def latest_rated_map(
        self,
        session: Session,
        *,
        title_type: str | None = None,
        title_filter: str | None = None,
    ) -> dict[tuple[int, int | None, int | None], int]:
        rows = self.list_filtered(
            session,
            title_type=title_type,
            title_filter=title_filter,
            action="rated",
        )
        rated_map: dict[tuple[int, int | None, int | None], int] = {}
        for row in rows:
            if row.rating is None:
                continue
            key = (row.title_trakt_id, row.season, row.episode)
            if key in rated_map:
                continue
            rated_map[key] = row.rating
        return rated_map

    def latest_show_episode_ratings(self, session: Session, show_trakt_id: int) -> dict[tuple[int, int], int]:
        rows = session.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.title_type == "show")
            .where(HistoryEvent.title_trakt_id == show_trakt_id)
            .where(HistoryEvent.season.is_not(None))
            .where(HistoryEvent.episode.is_not(None))
            .where(HistoryEvent.rating.is_not(None))
            .where(HistoryEvent.action.in_(("rated", "watched")))
            .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
        )
        result: dict[tuple[int, int], int] = {}
        for row in rows:
            if row.season is None or row.episode is None or row.rating is None:
                continue
            key = (int(row.season), int(row.episode))
            if key in result:
                continue
            result[key] = int(row.rating)
        return result

    def watched_episode_average_ratings(
        self,
        session: Session,
        show_trakt_ids: list[int],
    ) -> dict[int, float]:
        """Return per-show averages without materializing unrelated history."""
        show_ids = sorted({int(trakt_id) for trakt_id in show_trakt_ids})
        if not show_ids:
            return {}

        latest_order = (
            desc(HistoryEvent.watched_at_known),
            desc(HistoryEvent.watched_at),
            desc(HistoryEvent.id),
        )
        watched_ranked = (
            select(
                HistoryEvent.title_trakt_id.label("show_trakt_id"),
                HistoryEvent.season,
                HistoryEvent.episode,
                HistoryEvent.rating,
                func.row_number()
                .over(
                    partition_by=(HistoryEvent.title_trakt_id, HistoryEvent.season, HistoryEvent.episode),
                    order_by=latest_order,
                )
                .label("row_number"),
            )
            .where(HistoryEvent.title_type == "show")
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_trakt_id.in_(show_ids))
            .where(HistoryEvent.season.is_not(None))
            .where(HistoryEvent.episode.is_not(None))
            .subquery()
        )
        rated_ranked = (
            select(
                HistoryEvent.title_trakt_id.label("show_trakt_id"),
                HistoryEvent.season,
                HistoryEvent.episode,
                HistoryEvent.rating,
                func.row_number()
                .over(
                    partition_by=(HistoryEvent.title_trakt_id, HistoryEvent.season, HistoryEvent.episode),
                    order_by=latest_order,
                )
                .label("row_number"),
            )
            .where(HistoryEvent.title_type == "show")
            .where(HistoryEvent.action == "rated")
            .where(HistoryEvent.title_trakt_id.in_(show_ids))
            .where(HistoryEvent.season.is_not(None))
            .where(HistoryEvent.episode.is_not(None))
            .where(HistoryEvent.rating.is_not(None))
            .subquery()
        )
        watched = select(watched_ranked).where(watched_ranked.c.row_number == 1).subquery()
        rated = select(rated_ranked).where(rated_ranked.c.row_number == 1).subquery()
        display_rating = func.coalesce(watched.c.rating, rated.c.rating)
        rows = session.execute(
            select(
                watched.c.show_trakt_id,
                func.avg(display_rating).label("average_rating"),
            )
            .select_from(
                watched.outerjoin(
                    rated,
                    and_(
                        watched.c.show_trakt_id == rated.c.show_trakt_id,
                        watched.c.season == rated.c.season,
                        watched.c.episode == rated.c.episode,
                    ),
                )
            )
            .where(display_rating.is_not(None))
            .group_by(watched.c.show_trakt_id)
        )
        return {int(show_trakt_id): float(average_rating) for show_trakt_id, average_rating in rows}

    def apply_rating_to_latest_watch(
        self,
        session: Session,
        *,
        title_trakt_id: int,
        title_type: str,
        season: int | None,
        episode: int | None,
        rating: int,
    ) -> None:
        watched_row = session.scalar(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_trakt_id == title_trakt_id)
            .where(HistoryEvent.title_type == title_type)
            .where(HistoryEvent.season == season)
            .where(HistoryEvent.episode == episode)
            .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
            .limit(1)
        )
        if watched_row is not None:
            watched_row.rating = rating

    def list_recent(self, session: Session, limit: int = 20) -> list[HistoryEvent]:
        stmt = select(HistoryEvent).order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at)).limit(limit)
        return list(session.scalars(stmt))

    def list_filtered(
        self,
        session: Session,
        title_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        title_filter: str | None = None,
        action: str | None = None,
    ) -> list[HistoryEvent]:
        stmt = select(HistoryEvent)
        if action:
            stmt = stmt.where(HistoryEvent.action == action)
        if title_type:
            stmt = stmt.where(HistoryEvent.title_type == title_type)
        if title_filter:
            normalized_filter = normalize_title_search(title_filter)
            alias_match = exists(
                select(TitleAlias.id)
                .where(TitleAlias.title_type == HistoryEvent.title_type)
                .where(TitleAlias.title_trakt_id == HistoryEvent.title_trakt_id)
                .where(TitleAlias.normalized_title.like(f"%{normalized_filter}%"))
            )
            stmt = stmt.where(or_(HistoryEvent.title.ilike(f"%{title_filter}%"), alias_match))
        stmt = stmt.order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at))
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(session.scalars(stmt))

    def watched_episode_keys(self, session: Session, show_trakt_id: int) -> set[tuple[int, int]]:
        rows = session.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == "show")
            .where(HistoryEvent.title_trakt_id == show_trakt_id)
            .where(HistoryEvent.season.is_not(None))
            .where(HistoryEvent.episode.is_not(None))
        )
        return {
            (int(row.season), int(row.episode))
            for row in rows
            if row.season is not None and row.episode is not None
        }

    def watches_for_scope(
        self,
        session: Session,
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
    ) -> list[HistoryEvent]:
        stmt = (
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == title_type)
            .where(HistoryEvent.title_trakt_id == trakt_id)
        )
        if season is not None:
            stmt = stmt.where(HistoryEvent.season == season)
        return list(
            session.scalars(
                stmt.order_by(
                    desc(HistoryEvent.watched_at_known),
                    desc(HistoryEvent.watched_at),
                    desc(HistoryEvent.id),
                )
            )
        )

    def remove_watches_for_scope(
        self,
        session: Session,
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
    ) -> list[HistoryEvent]:
        rows = self.watches_for_scope(
            session,
            title_type=title_type,
            trakt_id=trakt_id,
            season=season,
        )
        for row in rows:
            session.delete(row)
        session.flush()
        return rows

    def watches_for_episode_keys(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        episode_keys: set[tuple[int, int]],
    ) -> list[HistoryEvent]:
        normalized_keys = {
            (int(season), int(episode))
            for season, episode in episode_keys
        }
        if not normalized_keys:
            return []
        return list(
            session.scalars(
                select(HistoryEvent)
                .where(HistoryEvent.action == "watched")
                .where(HistoryEvent.title_type == "show")
                .where(HistoryEvent.title_trakt_id == show_trakt_id)
                .where(tuple_(HistoryEvent.season, HistoryEvent.episode).in_(normalized_keys))
                .order_by(
                    desc(HistoryEvent.watched_at_known),
                    desc(HistoryEvent.watched_at),
                    desc(HistoryEvent.id),
                )
            )
        )

    def remove_watches_for_episode_keys(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        episode_keys: set[tuple[int, int]],
    ) -> list[HistoryEvent]:
        rows = self.watches_for_episode_keys(
            session,
            show_trakt_id=show_trakt_id,
            episode_keys=episode_keys,
        )
        for row in rows:
            session.delete(row)
        session.flush()
        return rows

    def remove_episode_watch(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        season: int,
        episode: int,
    ) -> HistoryEvent | None:
        rows = list(
            session.scalars(
                select(HistoryEvent)
                .where(HistoryEvent.action == "watched")
                .where(HistoryEvent.title_type == "show")
                .where(HistoryEvent.title_trakt_id == show_trakt_id)
                .where(HistoryEvent.season == season)
                .where(HistoryEvent.episode == episode)
                .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
            )
        )
        if not rows:
            return None
        removed = rows[0]
        for row in rows:
            session.delete(row)
        session.flush()
        return removed

    def episode_watch(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        season: int,
        episode: int,
    ) -> HistoryEvent | None:
        return session.scalar(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == "show")
            .where(HistoryEvent.title_trakt_id == show_trakt_id)
            .where(HistoryEvent.season == season)
            .where(HistoryEvent.episode == episode)
            .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
            .limit(1)
        )

    def latest_watch_for_title(self, session: Session, *, title_type: str, trakt_id: int) -> HistoryEvent | None:
        return session.scalar(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == title_type)
            .where(HistoryEvent.title_trakt_id == trakt_id)
            .order_by(desc(HistoryEvent.watched_at_known), desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
            .limit(1)
        )

    @staticmethod
    def _within_local_watch_window(left: datetime, right: datetime) -> bool:
        try:
            return abs(left - right) <= HistoryRepository._LOCAL_WATCH_DEDUP_WINDOW
        except TypeError:
            if left.tzinfo is not None:
                left = left.replace(tzinfo=None)
            if right.tzinfo is not None:
                right = right.replace(tzinfo=None)
            return abs(left - right) <= HistoryRepository._LOCAL_WATCH_DEDUP_WINDOW

    def distinct_titles(self, session: Session, title_type: str | None = None, action: str | None = None) -> list[str]:
        stmt = select(HistoryEvent.title).distinct().order_by(HistoryEvent.title)
        if action:
            stmt = stmt.where(HistoryEvent.action == action)
        if title_type:
            stmt = stmt.where(HistoryEvent.title_type == title_type)
        return [title.strip() for title in session.scalars(stmt) if title and title.strip()]

    def clear_ratings(self, session: Session) -> None:
        session.execute(delete(HistoryEvent).where(HistoryEvent.action == "rated"))
        session.execute(
            update(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .values(rating=None)
        )
        session.flush()

    def known_trakt_history_ids(self, session: Session) -> set[int]:
        stmt = select(HistoryEvent.trakt_history_id).where(HistoryEvent.trakt_history_id.is_not(None))
        return {int(history_id) for history_id in session.scalars(stmt) if history_id is not None}

    def delete_missing_trakt_watches(
        self,
        session: Session,
        *,
        title_type: str,
        present_history_ids: set[int],
        watched_at_cutoff: datetime | None = None,
    ) -> tuple[set[tuple[str, int]], int]:
        stmt = (
            select(HistoryEvent)
            .where(HistoryEvent.source == "trakt")
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == title_type)
            .where(HistoryEvent.trakt_history_id.is_not(None))
        )
        if watched_at_cutoff is not None:
            cutoff = watched_at_cutoff.replace(tzinfo=None) if watched_at_cutoff.tzinfo is not None else watched_at_cutoff
            stmt = stmt.where(HistoryEvent.watched_at >= cutoff)
        removed_title_keys: set[tuple[str, int]] = set()
        removed_count = 0
        for row in session.scalars(stmt).all():
            if row.trakt_history_id is not None and int(row.trakt_history_id) in present_history_ids:
                continue
            removed_title_keys.add((str(row.title_type), int(row.title_trakt_id)))
            session.delete(row)
            removed_count += 1
        return removed_title_keys, removed_count

    def has_watched_title(self, session: Session, *, title_type: str, trakt_id: int) -> bool:
        stmt = (
            select(HistoryEvent.id)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == title_type)
            .where(HistoryEvent.title_trakt_id == trakt_id)
            .limit(1)
        )
        return session.scalar(stmt) is not None

    def watched_title_keys(self, session: Session) -> set[tuple[str, int]]:
        stmt = (
            select(HistoryEvent.title_type, HistoryEvent.title_trakt_id)
            .where(HistoryEvent.action == "watched")
            .distinct()
        )
        return {(str(title_type), int(trakt_id)) for title_type, trakt_id in session.execute(stmt)}


class ProgressRepository:
    @staticmethod
    def normalize_view(
        view: ProgressView | str = ProgressView.ACTIVE,
        *,
        dropped_only: bool | None = None,
    ) -> ProgressView:
        if dropped_only is not None:
            return ProgressView.DROPPED if dropped_only else ProgressView.ACTIVE
        if isinstance(view, ProgressView):
            return view
        try:
            return ProgressView(str(view or "").strip().casefold())
        except ValueError:
            return ProgressView.ACTIVE

    @staticmethod
    def normalize_sort_mode(
        sort_mode: ProgressSortMode | str = ProgressSortMode.EPISODE_RELEASE,
    ) -> ProgressSortMode:
        if isinstance(sort_mode, ProgressSortMode):
            return sort_mode
        normalized = str(sort_mode or "").strip().casefold().replace(" ", "_")
        aliases = {
            "last_watched": ProgressSortMode.LAST_WATCHED,
            "episode_release": ProgressSortMode.EPISODE_RELEASE,
            "release_year": ProgressSortMode.RELEASE_YEAR,
        }
        return aliases.get(normalized, ProgressSortMode.EPISODE_RELEASE)

    @staticmethod
    def _apply_view_filter(stmt, view: ProgressView, *, include_paused: bool = False):
        not_dropped = and_(
            or_(UserTitleState.archived.is_(None), UserTitleState.archived.is_(False)),
            or_(UserTitleState.tracked.is_(None), UserTitleState.tracked.is_(True)),
        )
        if view is ProgressView.DROPPED:
            return stmt.where(or_(UserTitleState.archived.is_(True), UserTitleState.tracked.is_(False)))
        if view is ProgressView.PAUSED:
            return stmt.where(not_dropped).where(UserTitleState.paused.is_(True))
        stmt = stmt.where(not_dropped)
        if include_paused:
            return stmt
        return stmt.where(or_(UserTitleState.paused.is_(None), UserTitleState.paused.is_(False)))

    def delete_progress(self, session: Session, trakt_id: int) -> None:
        session.execute(delete(WatchProgress).where(WatchProgress.show_trakt_id == trakt_id))
        session.flush()

    def upsert_progress(self, session: Session, progress: ProgressSnapshot) -> WatchProgress:
        model = session.scalar(select(WatchProgress).where(WatchProgress.show_trakt_id == progress.trakt_id))
        show_title = progress.title
        if _is_show_title_fallback(show_title, progress.trakt_id):
            title_row = session.scalar(select(Title).where(Title.trakt_id == progress.trakt_id))
            if title_row is not None and title_row.title and not _is_show_title_fallback(title_row.title, progress.trakt_id):
                show_title = title_row.title
            elif model is not None and model.show_title and not _is_show_title_fallback(model.show_title, progress.trakt_id):
                show_title = model.show_title
        if model is None:
            model = WatchProgress(show_trakt_id=progress.trakt_id, show_title=show_title)
            session.add(model)
        model.show_title = show_title
        model.completed = progress.completed
        model.aired = progress.aired
        model.percent_completed = progress.percent_completed
        if progress.next_episode:
            model.next_episode_trakt_id = progress.next_episode.trakt_id
            model.next_episode_season = progress.next_episode.season
            model.next_episode_number = progress.next_episode.number
            model.next_episode_title = progress.next_episode.title
            model.next_episode_first_aired = progress.next_episode.first_aired
        else:
            model.next_episode_trakt_id = None
            model.next_episode_season = None
            model.next_episode_number = None
            model.next_episode_title = ""
            model.next_episode_first_aired = None
        if progress.last_episode:
            model.last_episode_trakt_id = progress.last_episode.trakt_id
            model.last_episode_season = progress.last_episode.season
            model.last_episode_number = progress.last_episode.number
            model.last_episode_title = progress.last_episode.title
            model.last_episode_first_aired = progress.last_episode.first_aired
        else:
            model.last_episode_trakt_id = None
            model.last_episode_season = None
            model.last_episode_number = None
            model.last_episode_title = ""
            model.last_episode_first_aired = None
        session.flush()
        return model

    def list_in_progress(
        self,
        session: Session,
        *,
        view: ProgressView | str = ProgressView.ACTIVE,
        sort_mode: ProgressSortMode | str = ProgressSortMode.EPISODE_RELEASE,
        descending: bool = True,
        dropped_only: bool | None = None,
        limit: int | None = 50,
    ) -> list[ProgressSnapshot]:
        normalized_view = self.normalize_view(view, dropped_only=dropped_only)
        normalized_sort = self.normalize_sort_mode(sort_mode)
        stmt = (
            select(WatchProgress, Title, EpisodeCache, UserTitleState)
            .outerjoin(Title, Title.trakt_id == WatchProgress.show_trakt_id)
            .outerjoin(UserTitleState, UserTitleState.title_id == Title.id)
            .outerjoin(
                EpisodeCache,
                (EpisodeCache.show_trakt_id == WatchProgress.show_trakt_id)
                & (EpisodeCache.season == WatchProgress.next_episode_season)
                & (EpisodeCache.number == WatchProgress.next_episode_number),
            )
            .where(WatchProgress.completed > 0)
            .where(WatchProgress.next_episode_trakt_id.is_not(None))
        )
        stmt = self._apply_view_filter(stmt, normalized_view)
        if normalized_sort is ProgressSortMode.LAST_WATCHED:
            sort_column = UserTitleState.last_watched_at
        elif normalized_sort is ProgressSortMode.RELEASE_YEAR:
            sort_column = Title.year
        else:
            sort_column = WatchProgress.next_episode_first_aired
        primary_order = desc(sort_column) if descending else sort_column.asc()
        stable_title = func.lower(func.coalesce(Title.title, WatchProgress.show_title))
        stmt = stmt.order_by(
            sort_column.is_(None),
            primary_order,
            stable_title.asc(),
            WatchProgress.show_trakt_id.asc(),
        )
        if limit is not None:
            stmt = stmt.limit(max(0, int(limit)))
        rows = list(session.execute(stmt))
        result: list[ProgressSnapshot] = []
        for row, title, next_episode_row, state in rows:
            show_title = row.show_title
            if (
                title is not None
                and title.title
                and _is_show_title_fallback(show_title, row.show_trakt_id)
                and not _is_show_title_fallback(title.title, row.show_trakt_id)
            ):
                show_title = title.title
            next_episode = None
            if row.next_episode_trakt_id:
                next_episode = EpisodeSummary(
                    trakt_id=row.next_episode_trakt_id,
                    season=row.next_episode_season or 0,
                    number=row.next_episode_number or 0,
                    title=row.next_episode_title,
                    still_url=next_episode_row.still_url if next_episode_row is not None else "",
                    still_status=(next_episode_row.still_status if next_episode_row is not None else ENRICH_STATUS_UNKNOWN),
                    still_refreshed_at=(next_episode_row.still_refreshed_at if next_episode_row is not None else None),
                    trakt_rating=next_episode_row.trakt_rating if next_episode_row is not None else None,
                    trakt_votes=next_episode_row.trakt_votes if next_episode_row is not None else None,
                    trakt_details_status=(next_episode_row.trakt_details_status if next_episode_row is not None else ENRICH_STATUS_UNKNOWN),
                    trakt_details_refreshed_at=(next_episode_row.trakt_details_refreshed_at if next_episode_row is not None else None),
                    imdb_id=next_episode_row.imdb_id if next_episode_row is not None else "",
                    imdb_rating=next_episode_row.imdb_rating if next_episode_row is not None else None,
                    imdb_votes=next_episode_row.imdb_votes if next_episode_row is not None else None,
                    imdb_season=next_episode_row.imdb_season if next_episode_row is not None else None,
                    imdb_episode=next_episode_row.imdb_episode if next_episode_row is not None else None,
                    imdb_status=(
                        ENRICH_STATUS_READY
                        if next_episode_row is not None and next_episode_row.imdb_rating is not None and next_episode_row.imdb_votes is not None
                        else (
                            ENRICH_STATUS_CHECKED_NO_DATA
                            if next_episode_row is not None and next_episode_row.imdb_id
                            else ENRICH_STATUS_UNKNOWN
                        )
                    ),
                    first_aired=row.next_episode_first_aired,
                )
            last_episode = None
            if row.last_episode_trakt_id:
                last_episode = EpisodeSummary(
                    trakt_id=row.last_episode_trakt_id,
                    season=row.last_episode_season or 0,
                    number=row.last_episode_number or 0,
                    title=row.last_episode_title,
                    first_aired=row.last_episode_first_aired,
                )
            result.append(
                ProgressSnapshot(
                    trakt_id=row.show_trakt_id,
                    title=show_title,
                    completed=row.completed,
                    aired=row.aired,
                    percent_completed=row.percent_completed,
                    slug=title.slug if title is not None else "",
                    next_episode=next_episode,
                    last_episode=last_episode,
                    poster_url=title.poster_url if title is not None else "",
                    poster_status=(title.poster_status if title is not None else ENRICH_STATUS_UNKNOWN),
                    poster_refreshed_at=(title.poster_refreshed_at if title is not None else None),
                    status=title.status if title is not None else "",
                    title_trakt_rating=(title.trakt_rating if title is not None else None),
                    title_trakt_votes=(title.trakt_votes if title is not None else None),
                    title_imdb_rating=(title.imdb_rating if title is not None else None),
                    title_imdb_votes=(title.imdb_votes if title is not None else None),
                    title_ratings_status=(title.ratings_status if title is not None else ENRICH_STATUS_UNKNOWN),
                    title_ratings_refreshed_at=(title.ratings_refreshed_at if title is not None else None),
                    is_dropped=(
                        (bool(state.archived) or state.tracked is False)
                        if state is not None
                        else normalized_view is ProgressView.DROPPED
                    ),
                    is_paused=(bool(state.paused) if state is not None else False),
                    last_watched_at=(state.last_watched_at if state is not None else None),
                    title_year=(title.year if title is not None else None),
                )
            )
        return result

    def list_sync_show_ids(
        self,
        session: Session,
        *,
        view: ProgressView | str = ProgressView.ACTIVE,
        include_paused: bool = False,
        dropped_only: bool | None = None,
    ) -> list[int]:
        normalized_view = self.normalize_view(view, dropped_only=dropped_only)
        stmt = (
            select(WatchProgress.show_trakt_id, UserTitleState)
            .outerjoin(Title, Title.trakt_id == WatchProgress.show_trakt_id)
            .outerjoin(UserTitleState, UserTitleState.title_id == Title.id)
            .order_by(WatchProgress.updated_at.desc(), WatchProgress.show_title)
        )
        stmt = self._apply_view_filter(stmt, normalized_view, include_paused=include_paused)
        seen: set[int] = set()
        result: list[int] = []
        for show_trakt_id, _state in session.execute(stmt):
            trakt_id = int(show_trakt_id)
            if trakt_id in seen:
                continue
            seen.add(trakt_id)
            result.append(trakt_id)
        return result

    def has_incomplete_rows(
        self,
        session: Session,
        *,
        view: ProgressView | str = ProgressView.ACTIVE,
        include_paused: bool = False,
        dropped_only: bool | None = None,
    ) -> bool:
        normalized_view = self.normalize_view(view, dropped_only=dropped_only)
        stmt = (
            select(WatchProgress.id)
            .outerjoin(Title, Title.trakt_id == WatchProgress.show_trakt_id)
            .outerjoin(UserTitleState, UserTitleState.title_id == Title.id)
            .where(WatchProgress.completed > 0)
            .where(WatchProgress.completed < WatchProgress.aired)
            .where(WatchProgress.next_episode_trakt_id.is_(None))
            .limit(1)
        )
        stmt = self._apply_view_filter(stmt, normalized_view, include_paused=include_paused)
        return session.scalar(stmt) is not None


class EpisodeRepository:
    _UNSET = object()

    def replace_show_episodes(self, session: Session, show_trakt_id: int, episodes: list[EpisodeSummary]) -> None:
        existing_rows = {
            (row.season, row.number): row
            for row in session.scalars(select(EpisodeCache).where(EpisodeCache.show_trakt_id == show_trakt_id))
        }
        session.execute(delete(EpisodeCache).where(EpisodeCache.show_trakt_id == show_trakt_id))
        for episode in episodes:
            existing = existing_rows.get((episode.season, episode.number))
            session.add(
                EpisodeCache(
                    show_trakt_id=show_trakt_id,
                    episode_trakt_id=episode.trakt_id,
                    season=episode.season,
                    number=episode.number,
                    title=episode.title,
                    still_url=episode.still_url or (existing.still_url if existing is not None else ""),
                    still_missing=(bool(existing.still_missing) if existing is not None and not episode.still_url else False),
                    trakt_rating=(
                        episode.trakt_rating if episode.trakt_rating is not None
                        else (existing.trakt_rating if existing is not None else None)
                    ),
                    trakt_votes=(
                        episode.trakt_votes if episode.trakt_votes is not None
                        else (existing.trakt_votes if existing is not None else None)
                    ),
                    imdb_id=episode.imdb_id or (existing.imdb_id if existing is not None else ""),
                    imdb_rating=(
                        episode.imdb_rating if episode.imdb_rating is not None
                        else (existing.imdb_rating if existing is not None else None)
                    ),
                    imdb_votes=(
                        episode.imdb_votes if episode.imdb_votes is not None
                        else (existing.imdb_votes if existing is not None else None)
                    ),
                    imdb_season=(
                        existing.imdb_season
                        if existing is not None and (not episode.imdb_id or episode.imdb_id == existing.imdb_id)
                        else None
                    ),
                    imdb_episode=(
                        existing.imdb_episode
                        if existing is not None and (not episode.imdb_id or episode.imdb_id == existing.imdb_id)
                        else None
                    ),
                    imdb_coordinates_revision=(
                        existing.imdb_coordinates_revision
                        if existing is not None and (not episode.imdb_id or episode.imdb_id == existing.imdb_id)
                        else ""
                    ),
                    imdb_match_status=(
                        "resolved"
                        if episode.imdb_id or (existing is not None and existing.imdb_id)
                        else (
                            existing.imdb_match_status
                            if existing is not None and existing.title == episode.title
                            else "unknown"
                        )
                    ),
                    imdb_match_attempt_key=(
                        ""
                        if episode.imdb_id or (existing is not None and existing.imdb_id)
                        else (
                            existing.imdb_match_attempt_key
                            if existing is not None and existing.title == episode.title
                            else ""
                        )
                    ),
                    still_status=(
                        ENRICH_STATUS_READY if episode.still_url
                        else (existing.still_status if existing is not None else (
                            ENRICH_STATUS_CHECKED_NO_DATA if getattr(existing, "still_missing", False) else ENRICH_STATUS_UNKNOWN
                        ))
                    ),
                    still_refreshed_at=(
                        episode.still_refreshed_at
                        or (existing.still_refreshed_at if existing is not None else None)
                        or (datetime.now(tz=UTC).replace(tzinfo=None) if episode.still_url else None)
                    ),
                    trakt_details_status=(
                        ENRICH_STATUS_READY
                        if episode.trakt_rating is not None and episode.trakt_votes is not None
                        else (existing.trakt_details_status if existing is not None else ENRICH_STATUS_UNKNOWN)
                    ),
                    overview=episode.overview,
                    runtime=episode.runtime,
                    first_aired=episode.first_aired,
                )
            )

    def list_upcoming(self, session: Session, limit: int = 20) -> list[CalendarEntry]:
        now = datetime.utcnow()
        stmt = (
            select(EpisodeCache, Title)
            .join(Title, Title.trakt_id == EpisodeCache.show_trakt_id)
            .where(EpisodeCache.first_aired.is_not(None))
            .where(EpisodeCache.first_aired >= now)
            .order_by(EpisodeCache.first_aired)
            .limit(limit)
        )
        result: list[CalendarEntry] = []
        for episode, title in session.execute(stmt):
            result.append(
                CalendarEntry(
                    show_trakt_id=title.trakt_id,
                    show_title=title.title,
                    episode=EpisodeSummary(
                        trakt_id=episode.episode_trakt_id,
                        season=episode.season,
                        number=episode.number,
                        title=episode.title,
                        first_aired=episode.first_aired,
                        runtime=episode.runtime,
                        overview=episode.overview,
                    ),
                    )
                )
        return result

    def upsert_episode(self, session: Session, show_trakt_id: int, episode: EpisodeSummary) -> EpisodeCache:
        row = session.scalar(
            select(EpisodeCache).where(
                EpisodeCache.show_trakt_id == show_trakt_id,
                EpisodeCache.season == episode.season,
                EpisodeCache.number == episode.number,
            )
        )
        is_new = row is None
        previous_title = row.title if row is not None else ""
        previous_imdb_id = row.imdb_id if row is not None else ""
        if row is None:
            row = EpisodeCache(
                show_trakt_id=show_trakt_id,
                episode_trakt_id=episode.trakt_id,
                season=episode.season,
                number=episode.number,
            )
            session.add(row)
        row.episode_trakt_id = episode.trakt_id
        row.title = episode.title or ""
        if episode.still_url:
            row.still_url = episode.still_url
            row.still_missing = False
            row.still_status = ENRICH_STATUS_READY
            row.still_refreshed_at = (
                episode.still_refreshed_at.replace(tzinfo=None)
                if episode.still_refreshed_at is not None and episode.still_refreshed_at.tzinfo is not None
                else episode.still_refreshed_at
            ) or row.still_refreshed_at or datetime.now(tz=UTC).replace(tzinfo=None)
        elif not row.still_status:
            row.still_status = ENRICH_STATUS_CHECKED_NO_DATA if row.still_missing else ENRICH_STATUS_UNKNOWN
        elif episode.still_refreshed_at is not None:
            row.still_refreshed_at = (
                episode.still_refreshed_at.replace(tzinfo=None)
                if episode.still_refreshed_at.tzinfo is not None
                else episode.still_refreshed_at
            )
        if episode.trakt_rating is not None:
            row.trakt_rating = episode.trakt_rating
        if episode.trakt_votes is not None:
            row.trakt_votes = episode.trakt_votes
        if row.trakt_rating is not None and row.trakt_votes is not None:
            row.trakt_details_status = ENRICH_STATUS_READY
        elif not row.trakt_details_status:
            row.trakt_details_status = ENRICH_STATUS_UNKNOWN
        if episode.imdb_id:
            if episode.imdb_id != previous_imdb_id:
                row.imdb_season = None
                row.imdb_episode = None
                row.imdb_coordinates_revision = ""
            row.imdb_id = episode.imdb_id
            row.imdb_match_status = "resolved"
            row.imdb_match_attempt_key = ""
        elif is_new:
            row.imdb_match_status = "unknown"
            row.imdb_match_attempt_key = ""
        elif not row.imdb_id and previous_title != row.title:
            row.imdb_match_status = "unknown"
            row.imdb_match_attempt_key = ""
        if episode.imdb_rating is not None:
            row.imdb_rating = episode.imdb_rating
        if episode.imdb_votes is not None:
            row.imdb_votes = episode.imdb_votes
        row.overview = episode.overview or ""
        row.runtime = episode.runtime
        row.first_aired = episode.first_aired
        session.flush()
        return row

    def find_episode(self, session: Session, show_trakt_id: int, season: int, episode: int) -> EpisodeCache | None:
        return session.scalar(
            select(EpisodeCache).where(
                EpisodeCache.show_trakt_id == show_trakt_id,
                EpisodeCache.season == season,
                EpisodeCache.number == episode,
            )
        )

    def list_all_with_imdb(self, session: Session) -> list[EpisodeCache]:
        return list(session.scalars(select(EpisodeCache).where(EpisodeCache.imdb_id != "")))

    def list_all_episodes(self, session: Session) -> list[EpisodeCache]:
        return list(session.scalars(select(EpisodeCache).order_by(EpisodeCache.show_trakt_id, EpisodeCache.season, EpisodeCache.number)))

    def list_show_episodes(self, session: Session, show_trakt_id: int) -> list[EpisodeCache]:
        return list(
            session.scalars(
                select(EpisodeCache)
                .where(EpisodeCache.show_trakt_id == show_trakt_id)
                .order_by(EpisodeCache.season, EpisodeCache.number)
            )
        )

    def list_artwork_batch(self, session: Session, *, after_id: int, limit: int) -> list[EpisodeCache]:
        stmt = (
            select(EpisodeCache)
            .where(EpisodeCache.id > max(0, int(after_id)))
            .where(EpisodeCache.still_url.is_not(None), EpisodeCache.still_url != "")
            .order_by(EpisodeCache.id)
            .limit(max(1, int(limit)))
        )
        return list(session.scalars(stmt))

    def titles_by_episode_keys(self, session: Session, keys: list[tuple[int, int, int]]) -> dict[tuple[int, int, int], str]:
        if not keys:
            return {}
        unique_keys = list(dict.fromkeys(keys))
        stmt = select(EpisodeCache).where(
            tuple_(EpisodeCache.show_trakt_id, EpisodeCache.season, EpisodeCache.number).in_(unique_keys)
        )
        result: dict[tuple[int, int, int], str] = {}
        for row in session.scalars(stmt):
            if row.title:
                result[(row.show_trakt_id, row.season, row.number)] = row.title
        return result

    def metadata_by_episode_keys(self, session: Session, keys: list[tuple[int, int, int]]) -> dict[tuple[int, int, int], dict]:
        if not keys:
            return {}
        unique_keys = list(dict.fromkeys(keys))
        stmt = select(EpisodeCache).where(
            tuple_(EpisodeCache.show_trakt_id, EpisodeCache.season, EpisodeCache.number).in_(unique_keys)
        )
        result: dict[tuple[int, int, int], dict] = {}
        for row in session.scalars(stmt):
            result[(row.show_trakt_id, row.season, row.number)] = {
                "title": row.title,
                "still_url": row.still_url,
                "still_missing": bool(row.still_missing or row.still_status == ENRICH_STATUS_CHECKED_NO_DATA),
                "still_status": row.still_status or ENRICH_STATUS_UNKNOWN,
                "still_refreshed_at": row.still_refreshed_at,
                "first_aired": row.first_aired,
                "trakt_rating": row.trakt_rating,
                "trakt_votes": row.trakt_votes,
                "trakt_details_status": row.trakt_details_status or ENRICH_STATUS_UNKNOWN,
                "trakt_details_refreshed_at": row.trakt_details_refreshed_at,
                "imdb_id": row.imdb_id,
                "imdb_rating": row.imdb_rating,
                "imdb_votes": row.imdb_votes,
                "imdb_season": row.imdb_season,
                "imdb_episode": row.imdb_episode,
                "imdb_coordinates_revision": row.imdb_coordinates_revision or "",
                "imdb_match_status": row.imdb_match_status or "unknown",
                "imdb_match_attempt_key": row.imdb_match_attempt_key or "",
            }
        return result

    def list_show_episode_metadata(self, session: Session, show_trakt_id: int) -> list[dict]:
        rows = session.scalars(
            select(EpisodeCache)
            .where(EpisodeCache.show_trakt_id == show_trakt_id)
            .order_by(EpisodeCache.season, EpisodeCache.number)
        )
        return [
            {
                "episode_trakt_id": row.episode_trakt_id,
                "season": row.season,
                "number": row.number,
                "title": row.title,
                "still_url": row.still_url,
                "still_missing": bool(row.still_missing or row.still_status == ENRICH_STATUS_CHECKED_NO_DATA),
                "still_status": row.still_status or ENRICH_STATUS_UNKNOWN,
                "still_refreshed_at": row.still_refreshed_at,
                "trakt_rating": row.trakt_rating,
                "trakt_votes": row.trakt_votes,
                "trakt_details_status": row.trakt_details_status or ENRICH_STATUS_UNKNOWN,
                "trakt_details_refreshed_at": row.trakt_details_refreshed_at,
                "imdb_id": row.imdb_id,
                "imdb_rating": row.imdb_rating,
                "imdb_votes": row.imdb_votes,
                "imdb_season": row.imdb_season,
                "imdb_episode": row.imdb_episode,
                "imdb_coordinates_revision": row.imdb_coordinates_revision or "",
                "imdb_match_status": row.imdb_match_status or "unknown",
                "imdb_match_attempt_key": row.imdb_match_attempt_key or "",
                "first_aired": row.first_aired,
            }
            for row in rows
        ]

    def list_episode_rating_refresh_candidates(self, session: Session, *, limit: int | None = None) -> list[dict]:
        stmt = (
            select(EpisodeCache)
            .where(EpisodeCache.first_aired.is_not(None))
            .order_by(EpisodeCache.first_aired.desc(), EpisodeCache.show_trakt_id, EpisodeCache.season, EpisodeCache.number)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = session.scalars(stmt)
        return [
            {
                "show_trakt_id": int(row.show_trakt_id),
                "season": int(row.season),
                "number": int(row.number),
                "first_aired": row.first_aired,
                "trakt_rating": row.trakt_rating,
                "trakt_votes": row.trakt_votes,
                "trakt_details_status": row.trakt_details_status or ENRICH_STATUS_UNKNOWN,
                "trakt_details_refreshed_at": row.trakt_details_refreshed_at,
            }
            for row in rows
        ]

    def list_cached_show_ids(self, session: Session) -> list[int]:
        stmt = select(EpisodeCache.show_trakt_id).distinct().order_by(EpisodeCache.show_trakt_id)
        return [int(show_id) for show_id in session.scalars(stmt)]

    def list_episode_keys(
        self,
        session: Session,
        *,
        statuses: tuple[str, ...] | None = None,
        include_missing_still: bool = False,
    ) -> list[tuple[int, int, int]]:
        stmt = select(EpisodeCache)
        if statuses:
            stmt = stmt.where(EpisodeCache.still_status.in_(statuses))
        if include_missing_still:
            stmt = stmt.where(or_(EpisodeCache.still_url == "", EpisodeCache.still_url.is_(None)))
        rows = list(session.scalars(stmt))
        return [(int(row.show_trakt_id), int(row.season), int(row.number)) for row in rows]

    def update_still_enrich_state(
        self,
        session: Session,
        show_trakt_id: int,
        season: int,
        episode: int,
        *,
        status: str,
        still_url: str | object = _UNSET,
    ) -> EpisodeCache | None:
        row = self.find_episode(session, show_trakt_id, season, episode)
        if row is None:
            return None
        if still_url is not self._UNSET:
            row.still_url = str(still_url or "")
        row.still_status = ENRICH_STATUS_READY if row.still_url else status
        row.still_missing = row.still_status == ENRICH_STATUS_CHECKED_NO_DATA
        row.still_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        session.flush()
        return row

    def update_trakt_details_enrich_state(
        self,
        session: Session,
        show_trakt_id: int,
        season: int,
        episode: int,
        *,
        status: str,
        details: EpisodeSummary | None = None,
    ) -> EpisodeCache | None:
        row = self.find_episode(session, show_trakt_id, season, episode)
        if row is None:
            return None
        if details is not None:
            row.episode_trakt_id = details.trakt_id or row.episode_trakt_id
            row.title = details.title or row.title
            row.overview = details.overview or row.overview
            row.runtime = details.runtime if details.runtime is not None else row.runtime
            row.first_aired = details.first_aired or row.first_aired
            if details.trakt_rating is not None:
                row.trakt_rating = details.trakt_rating
            if details.trakt_votes is not None:
                row.trakt_votes = details.trakt_votes
            if details.imdb_id:
                row.imdb_id = details.imdb_id
            if details.imdb_rating is not None:
                row.imdb_rating = details.imdb_rating
            if details.imdb_votes is not None:
                row.imdb_votes = details.imdb_votes
        if row.trakt_rating is not None and row.trakt_votes is not None:
            row.trakt_details_status = ENRICH_STATUS_READY
        else:
            row.trakt_details_status = status
        row.trakt_details_refreshed_at = datetime.now(tz=UTC).replace(tzinfo=None)
        session.flush()
        return row


class ReleaseTrackingRepository:
    def get(self, session: Session, title_type: str, trakt_id: int) -> ReleaseTrackingState | None:
        return session.scalar(
            select(ReleaseTrackingState).where(
                ReleaseTrackingState.title_type == title_type,
                ReleaseTrackingState.trakt_id == trakt_id,
            )
        )

    def sync_items(self, session: Session, items: list[TitleSummary]) -> None:
        active_keys = {(item.title_type, int(item.trakt_id)) for item in items}
        for row in list(session.scalars(select(ReleaseTrackingState))):
            if (row.title_type, int(row.trakt_id)) not in active_keys:
                session.delete(row)
        for item in items:
            row = self.get(session, item.title_type, int(item.trakt_id))
            if row is None:
                row = ReleaseTrackingState(title_type=item.title_type, trakt_id=int(item.trakt_id))
                session.add(row)
            row.title = item.title
            row.release_at = self._naive_utc(item.released_at)
        session.flush()

    def list_all(self, session: Session) -> list[ReleaseTrackingState]:
        return list(session.scalars(select(ReleaseTrackingState)))

    def set_acknowledged(self, session: Session, title_type: str, trakt_id: int, acknowledged: bool) -> bool:
        row = self.get(session, title_type, trakt_id)
        if row is None:
            raise RuntimeError("Release tracking item is not available locally yet.")
        row.acknowledged_at = datetime.utcnow() if acknowledged else None
        if not acknowledged:
            row.last_sent_at = None
        session.flush()
        return row.acknowledged_at is not None

    def set_list_count(self, session: Session, title_type: str, trakt_id: int, list_count: int | None) -> None:
        row = self.get(session, title_type, trakt_id)
        if row is None or list_count is None:
            return
        row.list_count = max(0, int(list_count))
        session.flush()

    def mark_sent(self, session: Session, title_type: str, trakt_id: int) -> None:
        row = self.get(session, title_type, trakt_id)
        if row is None:
            return
        row.last_sent_at = datetime.utcnow()
        row.notify_count = int(row.notify_count or 0) + 1
        session.flush()

    def delete(self, session: Session, title_type: str, trakt_id: int) -> None:
        row = self.get(session, title_type, trakt_id)
        if row is not None:
            session.delete(row)
            session.flush()

    def released_count(self, session: Session, *, now: datetime | None = None) -> int:
        current = self._naive_utc(now or datetime.now(tz=UTC))
        assert current is not None
        return sum(1 for row in self.list_all(session) if row.release_at is not None and row.release_at <= current)

    @staticmethod
    def _naive_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)


class NotificationRepository:
    def get_log(self, session: Session, show_trakt_id: int, episode_trakt_id: int) -> NotificationLog | None:
        stmt = select(NotificationLog).where(
            NotificationLog.show_trakt_id == show_trakt_id,
            NotificationLog.episode_trakt_id == episode_trakt_id,
        )
        return session.scalar(stmt)

    def was_sent(self, session: Session, show_trakt_id: int, episode_trakt_id: int) -> bool:
        return self.get_log(session, show_trakt_id, episode_trakt_id) is not None

    def mark_sent(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        show_title: str,
        episode_trakt_id: int,
        season: int,
        episode: int,
        message: str,
    ) -> None:
        row = self.get_log(session, show_trakt_id, episode_trakt_id)
        now = datetime.utcnow()
        if row is None:
            session.add(
                NotificationLog(
                    show_trakt_id=show_trakt_id,
                    show_title=show_title,
                    episode_trakt_id=episode_trakt_id,
                    season=season,
                    episode=episode,
                    sent_at=now,
                    last_sent_at=now,
                    notify_count=1,
                    message=message,
                )
            )
            return
        row.show_title = show_title
        row.season = season
        row.episode = episode
        row.message = message
        row.last_sent_at = now
        row.notify_count = max(1, row.notify_count or 0) + 1

    def track_released(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        show_title: str,
        episode_trakt_id: int,
        season: int,
        episode: int,
        message: str,
        released_at: datetime,
    ) -> None:
        row = self.get_log(session, show_trakt_id, episode_trakt_id)
        if row is not None:
            row.show_title = show_title
            row.season = season
            row.episode = episode
            row.message = message
            return
        stamp = released_at.replace(tzinfo=None) if released_at.tzinfo is not None else released_at
        session.add(
            NotificationLog(
                show_trakt_id=show_trakt_id,
                show_title=show_title,
                episode_trakt_id=episode_trakt_id,
                season=season,
                episode=episode,
                sent_at=stamp,
                last_sent_at=stamp,
                notify_count=0,
                message=message,
            )
        )

    def mark_seen(
        self,
        session: Session,
        *,
        show_trakt_id: int,
        show_title: str,
        episode_trakt_id: int,
        season: int,
        episode: int,
        message: str,
    ) -> None:
        row = self.get_log(session, show_trakt_id, episode_trakt_id)
        now = datetime.utcnow()
        if row is None:
            session.add(
                NotificationLog(
                    show_trakt_id=show_trakt_id,
                    show_title=show_title,
                    episode_trakt_id=episode_trakt_id,
                    season=season,
                    episode=episode,
                    sent_at=now,
                    last_sent_at=now,
                    seen_at=now,
                    notify_count=0,
                    message=message,
                )
            )
            return
        row.show_title = show_title
        row.season = season
        row.episode = episode
        row.message = message
        row.seen_at = now

    def unseen_episode_ids(self, session: Session) -> set[int]:
        stmt = select(NotificationLog.episode_trakt_id).where(NotificationLog.seen_at.is_(None))
        return {int(value) for value in session.scalars(stmt)}

    def list_unseen(self, session: Session) -> list[NotificationLog]:
        stmt = (
            select(NotificationLog)
            .where(NotificationLog.seen_at.is_(None))
            .order_by(NotificationLog.last_sent_at.asc(), NotificationLog.id.asc())
        )
        return list(session.scalars(stmt))

    def delete_sent(self, session: Session, show_trakt_id: int, episode_trakt_id: int) -> None:
        row = self.get_log(session, show_trakt_id, episode_trakt_id)
        if row is not None:
            session.delete(row)


class SyncStateRepository:
    def get_value(self, session: Session, key: str, default: str = "") -> str:
        state = session.scalar(select(SyncState).where(SyncState.key == key))
        return state.value if state else default

    def set_value(self, session: Session, key: str, value: str) -> None:
        state = session.scalar(select(SyncState).where(SyncState.key == key))
        if state is None:
            state = SyncState(key=key, value=value)
            session.add(state)
        else:
            state.value = value
