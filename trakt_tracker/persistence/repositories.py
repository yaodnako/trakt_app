from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, desc, or_, select, tuple_
from sqlalchemy.orm import Session

from trakt_tracker.domain import CalendarEntry, EpisodeSummary, ProgressSnapshot, TitleSummary

from .models import EpisodeCache, HistoryEvent, NotificationLog, SyncState, Title, UserTitleState, WatchProgress


class TitleRepository:
    def upsert_title(self, session: Session, title: TitleSummary) -> Title:
        model = session.scalar(select(Title).where(Title.trakt_id == title.trakt_id))
        if model is None:
            model = Title(trakt_id=title.trakt_id, title_type=title.title_type, title=title.title)
            session.add(model)
        model.title = title.title
        model.title_type = title.title_type
        model.year = title.year
        model.overview = title.overview
        model.poster_url = title.poster_url
        model.status = title.status
        model.slug = title.slug
        session.flush()
        return model

    def get_title(self, session: Session, trakt_id: int) -> Title | None:
        return session.scalar(select(Title).where(Title.trakt_id == trakt_id))

    def list_titles(self, session: Session) -> list[Title]:
        return list(session.scalars(select(Title).order_by(Title.title)))


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

    def set_archived(self, session: Session, trakt_id: int, archived: bool) -> None:
        title = session.scalar(select(Title).where(Title.trakt_id == trakt_id))
        if title is None:
            return
        state = self.ensure_state(session, title.id)
        state.archived = archived
        if archived:
            state.tracked = False
        else:
            state.tracked = True
        session.flush()

    def sync_progress_archived_states(self, session: Session, dropped_ids: set[int]) -> None:
        stmt = (
            select(Title)
            .join(WatchProgress, WatchProgress.show_trakt_id == Title.trakt_id)
            .where(Title.title_type == "show")
        )
        for title in session.scalars(stmt):
            state = self.ensure_state(session, title.id)
            is_dropped = title.trakt_id in dropped_ids
            state.archived = is_dropped
            state.tracked = not is_dropped
        session.flush()


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
    ) -> HistoryEvent | None:
        existing_local = session.scalar(
            select(HistoryEvent)
            .where(HistoryEvent.source == "local")
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_trakt_id == title_trakt_id)
            .where(HistoryEvent.season == season)
            .where(HistoryEvent.episode == episode)
            .order_by(desc(HistoryEvent.watched_at))
            .limit(1)
        )
        if (
            existing_local is not None
            and abs(watched_at - existing_local.watched_at) <= self._LOCAL_WATCH_DEDUP_WINDOW
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
                existing.season = season
                existing.episode = episode
                existing.rating = rating
                existing.source = source
                session.flush()
                return existing
        if trakt_history_id is None and source == "local" and action == "watched":
            existing_local = self.find_recent_local_watch(
                session,
                title_trakt_id=title_trakt_id,
                season=season,
                episode=episode,
                watched_at=watched_at,
            )
            if existing_local is not None:
                existing_local.title = title
                existing_local.title_type = title_type
                existing_local.watched_at = watched_at
                existing_local.rating = rating
                self._delete_other_watched_duplicates(session, existing_local)
                session.flush()
                return existing_local
        event = HistoryEvent(
            trakt_history_id=trakt_history_id,
            title_trakt_id=title_trakt_id,
            title=title,
            title_type=title_type,
            action=action,
            watched_at=watched_at,
            season=season,
            episode=episode,
            rating=rating,
            source=source,
        )
        session.add(event)
        session.flush()
        if action == "watched":
            self._delete_other_watched_duplicates(session, event)
        return event

    def _delete_other_watched_duplicates(self, session: Session, keep_event: HistoryEvent) -> None:
        duplicates = session.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .where(HistoryEvent.title_type == keep_event.title_type)
            .where(HistoryEvent.title_trakt_id == keep_event.title_trakt_id)
            .where(HistoryEvent.season == keep_event.season)
            .where(HistoryEvent.episode == keep_event.episode)
            .where(HistoryEvent.id != keep_event.id)
        ).all()
        for duplicate in duplicates:
            session.delete(duplicate)

    def collapse_duplicate_watches(self, session: Session) -> None:
        rows = session.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.action == "watched")
            .order_by(desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
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
            .order_by(desc(HistoryEvent.watched_at), desc(HistoryEvent.id))
            .limit(1)
        )
        if watched_row is not None:
            watched_row.rating = rating

    def list_recent(self, session: Session, limit: int = 20) -> list[HistoryEvent]:
        stmt = select(HistoryEvent).order_by(desc(HistoryEvent.watched_at)).limit(limit)
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
            stmt = stmt.where(HistoryEvent.title.ilike(f"%{title_filter}%"))
        stmt = stmt.order_by(desc(HistoryEvent.watched_at))
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(session.scalars(stmt))

    def distinct_titles(self, session: Session, title_type: str | None = None, action: str | None = None) -> list[str]:
        stmt = select(HistoryEvent.title).distinct().order_by(HistoryEvent.title)
        if action:
            stmt = stmt.where(HistoryEvent.action == action)
        if title_type:
            stmt = stmt.where(HistoryEvent.title_type == title_type)
        return [title.strip() for title in session.scalars(stmt) if title and title.strip()]

    def delete_trakt_rated(self, session: Session) -> None:
        session.execute(delete(HistoryEvent).where(HistoryEvent.source == "trakt", HistoryEvent.action == "rated"))

    def known_trakt_history_ids(self, session: Session) -> set[int]:
        stmt = select(HistoryEvent.trakt_history_id).where(HistoryEvent.trakt_history_id.is_not(None))
        return {int(history_id) for history_id in session.scalars(stmt) if history_id is not None}


class ProgressRepository:
    def upsert_progress(self, session: Session, progress: ProgressSnapshot) -> WatchProgress:
        model = session.scalar(select(WatchProgress).where(WatchProgress.show_trakt_id == progress.trakt_id))
        if model is None:
            model = WatchProgress(show_trakt_id=progress.trakt_id, show_title=progress.title)
            session.add(model)
        model.show_title = progress.title
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

    def list_in_progress(self, session: Session, *, dropped_only: bool = False) -> list[ProgressSnapshot]:
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
            .where(WatchProgress.next_episode_trakt_id.is_not(None))
            .order_by(
                WatchProgress.next_episode_first_aired.is_(None),
                desc(WatchProgress.next_episode_first_aired),
                WatchProgress.show_title,
            )
            .limit(50)
        )
        if dropped_only:
            stmt = stmt.where(or_(UserTitleState.archived.is_(True), UserTitleState.tracked.is_(False)))
        else:
            stmt = stmt.where(or_(UserTitleState.archived.is_(None), UserTitleState.archived.is_(False)))
            stmt = stmt.where(or_(UserTitleState.tracked.is_(None), UserTitleState.tracked.is_(True)))
        rows = list(session.execute(stmt))
        result: list[ProgressSnapshot] = []
        for row, title, next_episode_row, state in rows:
            next_episode = None
            if row.next_episode_trakt_id:
                next_episode = EpisodeSummary(
                    trakt_id=row.next_episode_trakt_id,
                    season=row.next_episode_season or 0,
                    number=row.next_episode_number or 0,
                    title=row.next_episode_title,
                    trakt_rating=next_episode_row.trakt_rating if next_episode_row is not None else None,
                    trakt_votes=next_episode_row.trakt_votes if next_episode_row is not None else None,
                    imdb_id=next_episode_row.imdb_id if next_episode_row is not None else "",
                    imdb_rating=next_episode_row.imdb_rating if next_episode_row is not None else None,
                    imdb_votes=next_episode_row.imdb_votes if next_episode_row is not None else None,
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
                    title=row.show_title,
                    completed=row.completed,
                    aired=row.aired,
                    percent_completed=row.percent_completed,
                    next_episode=next_episode,
                    last_episode=last_episode,
                    poster_url=title.poster_url if title is not None else "",
                    status=title.status if title is not None else "",
                    is_dropped=((bool(state.archived) or state.tracked is False) if state is not None else dropped_only),
                )
            )
        return result

    def list_sync_show_ids(self, session: Session, *, dropped_only: bool = False) -> list[int]:
        stmt = (
            select(WatchProgress.show_trakt_id, UserTitleState)
            .outerjoin(Title, Title.trakt_id == WatchProgress.show_trakt_id)
            .outerjoin(UserTitleState, UserTitleState.title_id == Title.id)
            .order_by(WatchProgress.updated_at.desc(), WatchProgress.show_title)
        )
        if dropped_only:
            stmt = stmt.where(or_(UserTitleState.archived.is_(True), UserTitleState.tracked.is_(False)))
        else:
            stmt = stmt.where(or_(UserTitleState.archived.is_(None), UserTitleState.archived.is_(False)))
            stmt = stmt.where(or_(UserTitleState.tracked.is_(None), UserTitleState.tracked.is_(True)))
        seen: set[int] = set()
        result: list[int] = []
        for show_trakt_id, _state in session.execute(stmt):
            trakt_id = int(show_trakt_id)
            if trakt_id in seen:
                continue
            seen.add(trakt_id)
            result.append(trakt_id)
        return result

    def has_incomplete_rows(self, session: Session, *, dropped_only: bool = False) -> bool:
        stmt = (
            select(WatchProgress.id)
            .outerjoin(Title, Title.trakt_id == WatchProgress.show_trakt_id)
            .outerjoin(UserTitleState, UserTitleState.title_id == Title.id)
            .where(WatchProgress.completed < WatchProgress.aired)
            .where(WatchProgress.next_episode_trakt_id.is_(None))
            .limit(1)
        )
        if dropped_only:
            stmt = stmt.where(or_(UserTitleState.archived.is_(True), UserTitleState.tracked.is_(False)))
        else:
            stmt = stmt.where(or_(UserTitleState.archived.is_(None), UserTitleState.archived.is_(False)))
            stmt = stmt.where(or_(UserTitleState.tracked.is_(None), UserTitleState.tracked.is_(True)))
        return session.scalar(stmt) is not None


class EpisodeRepository:
    def replace_show_episodes(self, session: Session, show_trakt_id: int, episodes: list[EpisodeSummary]) -> None:
        session.execute(delete(EpisodeCache).where(EpisodeCache.show_trakt_id == show_trakt_id))
        for episode in episodes:
            session.add(
                EpisodeCache(
                    show_trakt_id=show_trakt_id,
                    episode_trakt_id=episode.trakt_id,
                    season=episode.season,
                    number=episode.number,
                    title=episode.title,
                    imdb_id=episode.imdb_id,
                    imdb_rating=episode.imdb_rating,
                    imdb_votes=episode.imdb_votes,
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
        row.trakt_rating = episode.trakt_rating
        row.trakt_votes = episode.trakt_votes
        row.imdb_id = episode.imdb_id or ""
        row.imdb_rating = episode.imdb_rating
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
                "imdb_id": row.imdb_id,
                "imdb_rating": row.imdb_rating,
                "imdb_votes": row.imdb_votes,
            }
        return result


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
