from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path


APP_DIR_NAME = "TraktTracker"
CONFIG_FILE_NAME = "config.json"
TRAKT_CLIENT_ID_ENV = "TRAKT_TRACKER_TRAKT_CLIENT_ID"
TRAKT_CLIENT_SECRET_ENV = "TRAKT_TRACKER_TRAKT_CLIENT_SECRET"
TMDB_API_KEY_ENV = "TRAKT_TRACKER_TMDB_API_KEY"
TMDB_READ_ACCESS_TOKEN_ENV = "TRAKT_TRACKER_TMDB_READ_ACCESS_TOKEN"


def get_app_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    root = (Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local") / APP_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(slots=True)
class AppConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    tmdb_api_key: str = ""
    tmdb_read_access_token: str = ""
    kinopoisk_api_key: str = ""
    kinopoisk_domain_tail: str = "net"
    kinopoisk_domain_options: str = "net,ru"
    open_in_embedded_player: bool = False
    web_hide_spoilers: bool = False
    hide_upcoming_in_progress: bool = False
    show_paused_in_progress: bool = False
    show_dropped_in_progress: bool = False
    web_progress_sort_mode: str = "episode_release"
    web_progress_sort_direction: str = "desc"
    web_progress_min_year: int | None = None
    web_progress_year_filter_enabled: bool = False
    web_imdb_seasons_enabled: bool = True
    omdb_api_key: str = ""
    cache_ttl_hours: int = 24
    explore_imdb_scan_page_limit: int = 10
    poll_interval_minutes: int = 30
    notification_repeat_minutes: int = 5
    notification_release_delay_minutes: int = 120
    movie_release_notification_delay_minutes: int = 10080
    imdb_auto_sync_interval_minutes: int = 180
    imdb_auto_sync_interval_hours: int = 3
    notification_sound_path: str = ""
    notifications_enabled: bool = True
    debug_mode: bool = False
    web_portal_start_with_windows: bool = False
    utc_offset: str = "+03:00"
    window_x: int | None = None
    window_y: int | None = None
    window_width: int = 1100
    window_height: int = 760
    window_maximized: bool = False
    database_path: str = ""
    last_user_slug: str = ""
    active_profile_slug: str = ""
    known_profile_slugs: list[str] = field(default_factory=list)
    legacy_profile_migrated_slug: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def resolved_database_path(self) -> Path:
        slug = self.active_profile_slug or self.last_user_slug
        if slug:
            return profile_database_path(self, slug)
        return get_app_data_dir() / "bootstrap.sqlite3"

    @property
    def active_slug(self) -> str:
        return (self.active_profile_slug or self.last_user_slug).strip()


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (get_app_data_dir() / CONFIG_FILE_NAME)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AppConfig:
        if not self._path.exists():
            return AppConfig()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if "imdb_auto_sync_interval_minutes" not in raw:
            raw["imdb_auto_sync_interval_minutes"] = max(1, int(raw.get("imdb_auto_sync_interval_hours", 3) or 3) * 60)
        if "imdb_auto_sync_interval_hours" not in raw:
            raw["imdb_auto_sync_interval_hours"] = max(1, int(raw["imdb_auto_sync_interval_minutes"]) // 60 or 1)
        config = AppConfig(**raw)
        active_slug = config.active_profile_slug.strip() or config.last_user_slug.strip()
        config.active_profile_slug = active_slug
        config.last_user_slug = active_slug
        config.known_profile_slugs = normalize_profile_slugs(
            [*config.known_profile_slugs, *([active_slug] if active_slug else [])]
        )
        return config

    def save(self, config: AppConfig) -> None:
        active_slug = config.active_profile_slug.strip() or config.last_user_slug.strip()
        config.active_profile_slug = active_slug
        config.last_user_slug = active_slug
        config.known_profile_slugs = normalize_profile_slugs(
            [*config.known_profile_slugs, *([active_slug] if active_slug else [])]
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


def normalize_profile_slugs(values: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        slug = str(value or "").strip()
        key = slug.casefold()
        if not slug or key in seen:
            continue
        seen.add(key)
        normalized.append(slug)
    return normalized


def profile_storage_id(slug: str) -> str:
    normalized = str(slug or "").strip().casefold()
    readable = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip(".-") or "profile"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{readable[:48]}-{digest}"


def profile_root(config: AppConfig) -> Path:
    if config.database_path:
        return Path(config.database_path).expanduser().parent / "TraktTrackerProfiles"
    return get_app_data_dir() / "profiles"


def profile_database_path(config: AppConfig, slug: str) -> Path:
    return profile_root(config) / profile_storage_id(slug) / "tracker.sqlite3"


def legacy_database_path(config: AppConfig) -> Path:
    if config.database_path:
        return Path(config.database_path).expanduser()
    return get_app_data_dir() / "tracker.sqlite3"


def trakt_cache_provider(slug: str | None) -> str:
    normalized = str(slug or "").strip()
    return f"trakt/{profile_storage_id(normalized)}" if normalized else "trakt/bootstrap"


def resolved_trakt_client_id(config: AppConfig) -> str:
    return config.client_id.strip() or os.environ.get(TRAKT_CLIENT_ID_ENV, "").strip()


def resolved_trakt_client_secret(config: AppConfig) -> str:
    return config.client_secret.strip() or os.environ.get(TRAKT_CLIENT_SECRET_ENV, "").strip()


def resolved_tmdb_api_key(config: AppConfig) -> str:
    return config.tmdb_api_key.strip() or os.environ.get(TMDB_API_KEY_ENV, "").strip()


def resolved_tmdb_read_access_token(config: AppConfig) -> str:
    return config.tmdb_read_access_token.strip() or os.environ.get(TMDB_READ_ACCESS_TOKEN_ENV, "").strip()


def trakt_credentials_source(config: AppConfig) -> str:
    if config.client_id.strip() and config.client_secret.strip():
        return "override"
    if resolved_trakt_client_id(config) and resolved_trakt_client_secret(config):
        return "application"
    return "missing"


def normalize_utc_offset(value: str | None, fallback: str = "+03:00") -> str:
    raw = (value or "").strip().upper()
    if not raw:
        return fallback
    if raw == "Z":
        return "+00:00"
    sign = "+"
    if raw[0] in "+-":
        sign = raw[0]
        raw = raw[1:]
    if ":" in raw:
        parts = raw.split(":", 1)
    else:
        parts = [raw, "00"]
    if len(parts) != 2:
        return fallback
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hours <= 14 and 0 <= minutes < 60):
        return fallback
    return f"{sign}{hours:02d}:{minutes:02d}"


def timezone_from_utc_offset(value: str | None) -> timezone:
    normalized = normalize_utc_offset(value)
    sign = 1 if normalized[0] == "+" else -1
    hours = int(normalized[1:3])
    minutes = int(normalized[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def format_local_datetime(value: datetime | None, utc_offset: str | None) -> str:
    if value is None:
        return ""
    tz = timezone_from_utc_offset(utc_offset)
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=UTC)
    return normalized.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def normalize_kinopoisk_domain_tail(value: str | None, fallback: str = "net") -> str:
    raw = str(value or "").strip().casefold().strip(".")
    if raw.startswith("https://"):
        raw = raw[len("https://") :]
    elif raw.startswith("http://"):
        raw = raw[len("http://") :]
    if raw.startswith("www.kinopoisk."):
        raw = raw[len("www.kinopoisk.") :]
    elif raw.startswith("kinopoisk."):
        raw = raw[len("kinopoisk.") :]
    return raw or fallback


def normalize_kinopoisk_domain_options(value: str | None, fallback: tuple[str, ...] = ("net", "ru")) -> list[str]:
    raw = str(value or "")
    options: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        normalized = normalize_kinopoisk_domain_tail(part, fallback="")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        options.append(normalized)
    if options:
        return options
    return [item for item in fallback if item]
