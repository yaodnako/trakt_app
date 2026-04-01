from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta


class SyncPolicy:
    HISTORY_AUTO_SYNC_INTERVAL = timedelta(minutes=5)
    HISTORY_ACTIVITY_PROBE_INTERVAL = timedelta(seconds=45)
    PROGRESS_FULL_SYNC_INTERVAL = timedelta(minutes=30)

    HISTORY_PROBE_KEY = "history_last_probe_at"
    HISTORY_SIGNATURE_KEY = "history_activity_signature"
    HISTORY_LAST_SYNC_KEY = "history_last_sync_at"
    PROGRESS_SIGNATURE_KEY = "progress_activity_signature"
    PROGRESS_LAST_FULL_SYNC_KEY = "progress_last_full_sync_at"

    @staticmethod
    def parse_timestamp(raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    @classmethod
    def build_history_activity_signature(cls, payload: dict) -> str:
        if not payload:
            return ""
        episodes = payload.get("episodes", {})
        movies = payload.get("movies", {})
        shows = payload.get("shows", {})
        if not isinstance(episodes, dict):
            episodes = {}
        if not isinstance(movies, dict):
            movies = {}
        if not isinstance(shows, dict):
            shows = {}
        signature_payload = {
            "episodes": {
                "watched_at": episodes.get("watched_at", ""),
                "rated_at": episodes.get("rated_at", ""),
            },
            "movies": {
                "watched_at": movies.get("watched_at", ""),
                "rated_at": movies.get("rated_at", ""),
            },
            "shows": {
                "rated_at": shows.get("rated_at", ""),
            },
        }
        return json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def build_progress_activity_signature(cls, payload: dict) -> str:
        if not payload:
            return ""
        episodes = payload.get("episodes", {})
        shows = payload.get("shows", {})
        if not isinstance(episodes, dict):
            episodes = {}
        if not isinstance(shows, dict):
            shows = {}
        signature_payload = {
            "episodes": {
                "watched_at": episodes.get("watched_at", ""),
                "rated_at": episodes.get("rated_at", ""),
            },
            "shows": {
                "rated_at": shows.get("rated_at", ""),
                "hidden_at": shows.get("hidden_at", ""),
                "dropped_at": shows.get("dropped_at", ""),
            },
        }
        return json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def should_probe_history(cls, last_probe_at_raw: str) -> bool:
        last_probe_at = cls.parse_timestamp(last_probe_at_raw)
        if last_probe_at is None:
            return True
        return datetime.now(tz=UTC) - last_probe_at >= cls.HISTORY_ACTIVITY_PROBE_INTERVAL

    @classmethod
    def should_run_history_sync(
        cls,
        *,
        current_signature: str,
        previous_signature: str,
        last_sync_at_raw: str,
    ) -> bool:
        if not current_signature:
            return False
        if current_signature != previous_signature:
            return True
        last_sync_at = cls.parse_timestamp(last_sync_at_raw)
        if last_sync_at is None:
            return True
        return datetime.now(tz=UTC) - last_sync_at >= cls.HISTORY_AUTO_SYNC_INTERVAL

    @classmethod
    def can_skip_full_progress_sync(
        cls,
        *,
        has_incomplete_rows: bool,
        current_signature: str,
        previous_signature: str,
        last_full_sync_raw: str,
    ) -> bool:
        if has_incomplete_rows:
            return False
        if not current_signature or current_signature != previous_signature:
            return False
        last_full_sync_at = cls.parse_timestamp(last_full_sync_raw)
        if last_full_sync_at is None:
            return False
        return datetime.now(tz=UTC) - last_full_sync_at < cls.PROGRESS_FULL_SYNC_INTERVAL
