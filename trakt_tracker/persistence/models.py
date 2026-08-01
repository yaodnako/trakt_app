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
    backdrop_url: Mapped[str] = mapped_column(String(512), default="")
    trakt_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    trakt_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmdb_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str] = mapped_column(String(32), default="")
    imdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    imdb_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    poster_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    backdrop_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    backdrop_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ratings_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    ratings_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
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
    imdb_season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_coordinates_revision: Mapped[str] = mapped_column(String(64), default="")
    imdb_match_status: Mapped[str] = mapped_column(String(32), default="unknown")
    imdb_match_attempt_key: Mapped[str] = mapped_column(String(64), default="")
    still_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    still_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trakt_details_status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    trakt_details_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    watched_at_known: Mapped[bool] = mapped_column(Boolean, default=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="local")


class TitleAlias(Base):
    __tablename__ = "title_aliases"
    __table_args__ = (
        UniqueConstraint(
            "title_type",
            "title_trakt_id",
            "language",
            "normalized_title",
            name="uq_title_alias_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    title_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    language: Mapped[str] = mapped_column(String(8), index=True)
    title: Mapped[str] = mapped_column(String(255))
    normalized_title: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32), default="trakt")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TitleAliasRefresh(Base):
    __tablename__ = "title_alias_refresh"
    __table_args__ = (
        UniqueConstraint(
            "title_type",
            "title_trakt_id",
            "language",
            name="uq_title_alias_refresh_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    title_trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    language: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(32), default=ENRICH_STATUS_UNKNOWN)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)


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


class ReleaseTrackingState(Base):
    __tablename__ = "release_tracking_state"
    __table_args__ = (UniqueConstraint("title_type", "trakt_id", name="uq_release_tracking_title"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    trakt_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    release_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    list_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TraktOutboxItem(Base):
    __tablename__ = "trakt_outbox"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    operation_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    operation_type: Mapped[str] = mapped_column(String(32), index=True)
    base_state_json: Mapped[str] = mapped_column(Text, default="null")
    desired_state_json: Mapped[str] = mapped_column(Text, default="null")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    dependency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    origin: Mapped[str] = mapped_column(String(32), default="user")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CatalogIdentityMap(Base):
    """Provider identity crosswalk used by the reversible TMDb catalog preview."""

    __tablename__ = "catalog_identity_map"
    __table_args__ = (
        UniqueConstraint("provider", "title_type", "provider_id", name="uq_catalog_identity_provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16), index=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    provider_id: Mapped[int] = mapped_column(Integer, index=True)
    trakt_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    imdb_id: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TmdbPreviewSnapshot(Base):
    """Durable card snapshot for TMDb-only local actions."""

    __tablename__ = "tmdb_preview_snapshots"
    __table_args__ = (
        UniqueConstraint("title_type", "tmdb_id", name="uq_tmdb_preview_snapshot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    trakt_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    imdb_id: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(64), default="")
    poster_url: Mapped[str] = mapped_column(String(512), default="")
    backdrop_url: Mapped[str] = mapped_column(String(512), default="")
    tmdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmdb_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    popularity: Mapped[float | None] = mapped_column(Float, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TmdbPreviewIntent(Base):
    """Local desired state for TMDb preview actions awaiting optional Trakt mapping."""

    __tablename__ = "tmdb_preview_intents"
    __table_args__ = (
        UniqueConstraint(
            "operation_type",
            "title_type",
            "tmdb_id",
            "season",
            "episode",
            name="uq_tmdb_preview_intent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    operation_type: Mapped[str] = mapped_column(String(32), index=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_state_json: Mapped[str] = mapped_column(Text, default="false")
    desired_state_json: Mapped[str] = mapped_column(Text, default="false")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="local_only", index=True)
    mapped_trakt_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TmdbPreviewReleaseState(Base):
    """Notification/release projection for TMDb-only tracked titles."""

    __tablename__ = "tmdb_preview_release_state"
    __table_args__ = (
        UniqueConstraint("title_type", "tmdb_id", name="uq_tmdb_preview_release"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title_type: Mapped[str] = mapped_column(String(16), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    release_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    list_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
