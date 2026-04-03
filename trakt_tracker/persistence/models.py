from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from trakt_tracker.application.enrich_state import ENRICH_STATUS_UNKNOWN


class Base(DeclarativeBase):
    pass


class Title(Base):
    __tablename__ = "titles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trakt_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(64), default="")
    poster_url: Mapped[str] = mapped_column(String(512), default="")
    trakt_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    trakt_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmdb_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str] = mapped_column(String(32), default="")
    imdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    imdb_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    ratings_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user_state: Mapped["UserTitleState | None"] = relationship(back_populates="title_obj", uselist=False)


class UserTitleState(Base):
    __tablename__ = "user_title_state"
    __table_args__ = (UniqueConstraint("title_id", name="uq_user_title_state_title"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), index=True)
    in_history: Mapped[bool] = mapped_column(Boolean, default=False)
    tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    title_obj: Mapped[Title] = relationship(back_populates="user_state")


class EpisodeCache(Base):
    __tablename__ = "episodes_cache"
    __table_args__ = (UniqueConstraint("show_trakt_id", "season", "number", name="uq_episode_identity"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    show_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    episode_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    season: Mapped[int] = mapped_column(Integer)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255), default="")
    still_url: Mapped[str] = mapped_column(String(512), default="")
    still_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    trakt_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    trakt_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str] = mapped_column(String(32), default="")
    imdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    imdb_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    still_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    trakt_details_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    overview: Mapped[str] = mapped_column(Text, default="")
    runtime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_aired: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class WatchProgress(Base):
    __tablename__ = "watch_progress"
    __table_args__ = (UniqueConstraint("show_trakt_id", name="uq_watch_progress_show"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    show_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    show_title: Mapped[str] = mapped_column(String(255))
    completed: Mapped[int] = mapped_column(Integer, default=0)
    aired: Mapped[int] = mapped_column(Integer, default=0)
    percent_completed: Mapped[float] = mapped_column(Float, default=0.0)
    next_episode_trakt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_episode_season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_episode_title: Mapped[str] = mapped_column(String(255), default="")
    next_episode_first_aired: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_episode_trakt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_episode_season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_episode_title: Mapped[str] = mapped_column(String(255), default="")
    last_episode_first_aired: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HistoryEvent(Base):
    __tablename__ = "history_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trakt_history_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255))
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(32))
    watched_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="local")


class NotificationLog(Base):
    __tablename__ = "notifications_log"
    __table_args__ = (UniqueConstraint("show_trakt_id", "episode_trakt_id", name="uq_notification_episode"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    show_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    show_title: Mapped[str] = mapped_column(String(255))
    episode_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    season: Mapped[int] = mapped_column(Integer)
    episode: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=1)
    message: Mapped[str] = mapped_column(String(512), default="")


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
