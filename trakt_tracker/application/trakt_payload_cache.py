from __future__ import annotations

import json

from trakt_tracker.config import get_app_data_dir


def load_cached_trakt_history_items() -> list[dict]:
    cache_dir = get_app_data_dir() / "cache" / "trakt"
    if not cache_dir.exists():
        return []
    merged_payload: list[dict] = []
    seen_keys: set[tuple] = set()
    for path in cache_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        value = payload.get("value")
        if not isinstance(value, list) or not value:
            continue
        first = value[0]
        if not isinstance(first, dict):
            continue
        if "watched_at" not in first or "type" not in first:
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("id"),
                item.get("watched_at"),
                item.get("type"),
                ((item.get("show") or {}).get("ids") or {}).get("trakt"),
                ((item.get("episode") or {}).get("ids") or {}).get("trakt"),
                ((item.get("movie") or {}).get("ids") or {}).get("trakt"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_payload.append(item)
    return merged_payload


def load_cached_trakt_rating_items() -> list[dict]:
    cache_dir = get_app_data_dir() / "cache" / "trakt"
    if not cache_dir.exists():
        return []
    merged_payload: list[dict] = []
    seen_keys: set[tuple] = set()
    for path in cache_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        value = payload.get("value")
        if not isinstance(value, list) or not value:
            continue
        first = value[0]
        if not isinstance(first, dict):
            continue
        if "rated_at" not in first or "type" not in first or "rating" not in first:
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("rated_at"),
                item.get("type"),
                item.get("rating"),
                ((item.get("show") or {}).get("ids") or {}).get("trakt"),
                ((item.get("episode") or {}).get("ids") or {}).get("trakt"),
                ((item.get("movie") or {}).get("ids") or {}).get("trakt"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_payload.append(item)
    return merged_payload
