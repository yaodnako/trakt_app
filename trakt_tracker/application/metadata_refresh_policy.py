from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
    ENRICH_STATUS_UNKNOWN,
)


ASSET_KIND_TITLE_RATINGS = "title_ratings"
ASSET_KIND_EPISODE_RATINGS = "episode_ratings"
ASSET_KIND_POSTER = "poster"
ASSET_KIND_BACKDROP = "backdrop"
ASSET_KIND_STILL = "still"

TRIGGER_VIEWPORT = "viewport"
TRIGGER_PAGE_CONTEXT = "page_context"
TRIGGER_VISIBLE_RATINGS_REFRESH = "visible_ratings_refresh"
TRIGGER_SYNC_EVENT = "sync_event"
TRIGGER_MANUAL_REPAIR = "manual_repair"
TRIGGER_BACKGROUND_SWEEP = "background_sweep"

TITLE_ALLOWED_PARTS = (ASSET_KIND_POSTER, ASSET_KIND_BACKDROP, ASSET_KIND_TITLE_RATINGS)
EPISODE_ALLOWED_PARTS = (ASSET_KIND_STILL, ASSET_KIND_EPISODE_RATINGS)

TITLE_RATINGS_READY_REFRESH_SECONDS = 300
EPISODE_RATINGS_READY_REFRESH_SECONDS = 300
RATINGS_EMPTY_REFRESH_SECONDS = 21600
RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS = 1800
ARTWORK_EMPTY_REFRESH_SECONDS = 604800
ARTWORK_RETRYABLE_FAILURE_BACKOFF_SECONDS = 21600
STILL_VISIBLE_EMPTY_REFRESH_SECONDS = 300
STILL_RECENT_EMPTY_REFRESH_SECONDS = 3600
STILL_RECENT_RELEASE_WINDOW_SECONDS = 1209600
EPISODE_RATINGS_FOREGROUND_WINDOW_SECONDS = 864000
EPISODE_RATINGS_BACKGROUND_BUCKET_10_TO_60_SECONDS = 21600
EPISODE_RATINGS_BACKGROUND_BUCKET_60_TO_180_SECONDS = 172800
EPISODE_RATINGS_BACKGROUND_BUCKET_180_TO_720_SECONDS = 1209600
EPISODE_RATINGS_BACKGROUND_BUCKET_720_PLUS_SECONDS = 5184000


@dataclass(frozen=True, slots=True)
class MetadataRefreshDecision:
    asset_kind: str
    trigger: str
    should_refresh: bool
    reason: str


@dataclass(frozen=True, slots=True)
class MetadataRefreshRequest:
    trigger: str
    requested_parts: tuple[str, ...] = ()

    def to_payload(self) -> dict:
        return {
            "trigger": self.trigger,
            "requested_parts": list(self.requested_parts),
        }


def normalize_refresh_trigger(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {
        TRIGGER_VIEWPORT,
        TRIGGER_PAGE_CONTEXT,
        TRIGGER_VISIBLE_RATINGS_REFRESH,
        TRIGGER_SYNC_EVENT,
        TRIGGER_MANUAL_REPAIR,
        TRIGGER_BACKGROUND_SWEEP,
    }:
        return normalized
    return TRIGGER_VIEWPORT


def normalize_requested_parts(parts, allowed_parts: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(parts, (list, tuple, set)):
        return ()
    normalized = []
    for part in parts:
        value = str(part or "").strip().lower()
        if value in allowed_parts and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def build_refresh_request(
    *,
    trigger: str,
    requested_parts,
    allowed_parts: tuple[str, ...],
) -> MetadataRefreshRequest:
    return MetadataRefreshRequest(
        trigger=normalize_refresh_trigger(trigger),
        requested_parts=normalize_requested_parts(requested_parts, allowed_parts),
    )


def refresh_requests_from_payload(raw_requests, *, allowed_parts: tuple[str, ...]) -> tuple[MetadataRefreshRequest, ...]:
    if not isinstance(raw_requests, (list, tuple)):
        return ()
    normalized: list[MetadataRefreshRequest] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw in raw_requests:
        if not isinstance(raw, dict):
            continue
        request = build_refresh_request(
            trigger=str(raw.get("trigger", "")),
            requested_parts=raw.get("requested_parts", []),
            allowed_parts=allowed_parts,
        )
        key = (request.trigger, request.requested_parts)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(request)
    return tuple(normalized)


def merge_refresh_requests(
    existing_requests,
    incoming_requests,
    *,
    allowed_parts: tuple[str, ...],
) -> list[dict]:
    merged = [
        *refresh_requests_from_payload(existing_requests, allowed_parts=allowed_parts),
        *refresh_requests_from_payload(incoming_requests, allowed_parts=allowed_parts),
    ]
    deduped: list[MetadataRefreshRequest] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for request in merged:
        key = (request.trigger, request.requested_parts)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(request)
    return [request.to_payload() for request in deduped]


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def metadata_refresh_due(
    asset_kind: str,
    *,
    status: str | None,
    last_checked_at: datetime | None,
    has_value: bool,
    trigger: str,
    first_aired: datetime | None = None,
    now: datetime | None = None,
) -> MetadataRefreshDecision:
    normalized_trigger = normalize_refresh_trigger(trigger)
    normalized_status = (status or "").strip().lower() or ENRICH_STATUS_UNKNOWN
    checked_at = normalize_datetime(last_checked_at)
    now_value = normalize_datetime(now) or datetime.now(tz=UTC)

    if normalized_trigger == TRIGGER_MANUAL_REPAIR:
        return MetadataRefreshDecision(
            asset_kind=asset_kind,
            trigger=normalized_trigger,
            should_refresh=True,
            reason="manual_repair_override",
        )

    if asset_kind in {ASSET_KIND_TITLE_RATINGS, ASSET_KIND_EPISODE_RATINGS}:
        return _ratings_refresh_due(
            asset_kind,
            status=normalized_status,
            last_checked_at=checked_at,
            has_value=has_value,
            trigger=normalized_trigger,
            first_aired=normalize_datetime(first_aired),
            now=now_value,
        )
    if asset_kind in {ASSET_KIND_POSTER, ASSET_KIND_BACKDROP, ASSET_KIND_STILL}:
        return _artwork_refresh_due(
            asset_kind,
            status=normalized_status,
            last_checked_at=checked_at,
            has_value=has_value,
            trigger=normalized_trigger,
            first_aired=normalize_datetime(first_aired),
            now=now_value,
        )
    return MetadataRefreshDecision(
        asset_kind=asset_kind,
        trigger=normalized_trigger,
        should_refresh=False,
        reason="unsupported_asset_kind",
    )


def _ratings_refresh_due(
    asset_kind: str,
    *,
    status: str,
    last_checked_at: datetime | None,
    has_value: bool,
    trigger: str,
    first_aired: datetime | None,
    now: datetime,
) -> MetadataRefreshDecision:
    if asset_kind == ASSET_KIND_EPISODE_RATINGS:
        return _episode_ratings_refresh_due(
            status=status,
            last_checked_at=last_checked_at,
            has_value=has_value,
            trigger=trigger,
            first_aired=first_aired,
            now=now,
        )
    if trigger not in {TRIGGER_VIEWPORT, TRIGGER_VISIBLE_RATINGS_REFRESH, TRIGGER_SYNC_EVENT}:
        return MetadataRefreshDecision(asset_kind, trigger, False, "trigger_not_supported")
    if status == ENRICH_STATUS_UNKNOWN:
        return MetadataRefreshDecision(asset_kind, trigger, True, "status_unknown")
    if status == ENRICH_STATUS_RETRYABLE_FAILURE:
        return _timed_refresh_decision(
            asset_kind,
            trigger,
            last_checked_at,
            timedelta(seconds=RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS),
            now,
            "retryable_failure_backoff_elapsed",
            "retryable_failure_backoff_active",
        )
    if status == ENRICH_STATUS_CHECKED_NO_DATA:
        return _timed_refresh_decision(
            asset_kind,
            trigger,
            last_checked_at,
            timedelta(seconds=RATINGS_EMPTY_REFRESH_SECONDS),
            now,
            "checked_no_data_ttl_elapsed",
            "checked_no_data_ttl_active",
        )
    if status == ENRICH_STATUS_READY:
        if trigger != TRIGGER_VISIBLE_RATINGS_REFRESH:
            return MetadataRefreshDecision(asset_kind, trigger, False, "ready_not_requested")
        ttl = timedelta(
            seconds=(
                TITLE_RATINGS_READY_REFRESH_SECONDS
                if asset_kind == ASSET_KIND_TITLE_RATINGS
                else EPISODE_RATINGS_READY_REFRESH_SECONDS
            )
        )
        return _timed_refresh_decision(
            asset_kind,
            trigger,
            last_checked_at,
            ttl,
            now,
            "ready_ttl_elapsed",
            "ready_ttl_active",
        )
    if has_value:
        return MetadataRefreshDecision(asset_kind, trigger, False, "value_present_without_refresh_status")
    return MetadataRefreshDecision(asset_kind, trigger, True, "missing_value")


def _episode_ratings_refresh_due(
    *,
    status: str,
    last_checked_at: datetime | None,
    has_value: bool,
    trigger: str,
    first_aired: datetime | None,
    now: datetime,
) -> MetadataRefreshDecision:
    if trigger not in {TRIGGER_VIEWPORT, TRIGGER_VISIBLE_RATINGS_REFRESH, TRIGGER_SYNC_EVENT, TRIGGER_BACKGROUND_SWEEP}:
        return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "trigger_not_supported")
    if first_aired is not None and first_aired > now:
        return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "episode_unreleased")
    foreground_allowed = _episode_ratings_foreground_window_allows(first_aired, now)
    if trigger == TRIGGER_BACKGROUND_SWEEP:
        ttl, due_reason, not_due_reason = _episode_ratings_background_ttl(first_aired, now)
        if ttl is None:
            return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, not_due_reason)
    else:
        if trigger == TRIGGER_VISIBLE_RATINGS_REFRESH and not foreground_allowed:
            return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "background_refresh_window")
        ttl = timedelta(seconds=EPISODE_RATINGS_READY_REFRESH_SECONDS)
        due_reason = "ready_ttl_elapsed"
        not_due_reason = "ready_ttl_active"
    if status == ENRICH_STATUS_UNKNOWN:
        if trigger == TRIGGER_BACKGROUND_SWEEP or foreground_allowed or trigger in {TRIGGER_VIEWPORT, TRIGGER_SYNC_EVENT}:
            return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, True, "status_unknown")
        return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "background_refresh_window")
    if status == ENRICH_STATUS_RETRYABLE_FAILURE:
        if trigger == TRIGGER_BACKGROUND_SWEEP or foreground_allowed or trigger in {TRIGGER_VIEWPORT, TRIGGER_SYNC_EVENT}:
            return _timed_refresh_decision(
                ASSET_KIND_EPISODE_RATINGS,
                trigger,
                last_checked_at,
                timedelta(seconds=RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS),
                now,
                "retryable_failure_backoff_elapsed",
                "retryable_failure_backoff_active",
            )
        return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "background_refresh_window")
    if status == ENRICH_STATUS_CHECKED_NO_DATA:
        return _timed_refresh_decision(
            ASSET_KIND_EPISODE_RATINGS,
            trigger,
            last_checked_at,
            ttl,
            now,
            due_reason if trigger == TRIGGER_BACKGROUND_SWEEP else "ready_ttl_elapsed",
            not_due_reason if trigger == TRIGGER_BACKGROUND_SWEEP else "ready_ttl_active",
        )
    if status == ENRICH_STATUS_READY:
        if trigger not in {TRIGGER_VISIBLE_RATINGS_REFRESH, TRIGGER_BACKGROUND_SWEEP}:
            return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "ready_not_requested")
        return _timed_refresh_decision(
            ASSET_KIND_EPISODE_RATINGS,
            trigger,
            last_checked_at,
            ttl,
            now,
            due_reason,
            not_due_reason,
        )
    if has_value:
        return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "value_present_without_refresh_status")
    if trigger == TRIGGER_BACKGROUND_SWEEP or foreground_allowed or trigger in {TRIGGER_VIEWPORT, TRIGGER_SYNC_EVENT}:
        return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, True, "missing_value")
    return MetadataRefreshDecision(ASSET_KIND_EPISODE_RATINGS, trigger, False, "background_refresh_window")


def _episode_ratings_foreground_window_allows(first_aired: datetime | None, now: datetime) -> bool:
    if first_aired is None:
        return True
    if first_aired > now:
        return False
    return now - first_aired <= timedelta(seconds=EPISODE_RATINGS_FOREGROUND_WINDOW_SECONDS)


def _episode_ratings_background_ttl(
    first_aired: datetime | None,
    now: datetime,
) -> tuple[timedelta | None, str, str]:
    if first_aired is None:
        return (
            timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_720_PLUS_SECONDS),
            "background_720_plus_ttl_elapsed",
            "background_720_plus_ttl_active",
        )
    age = now - first_aired
    if age <= timedelta(seconds=EPISODE_RATINGS_FOREGROUND_WINDOW_SECONDS):
        return (None, "background_foreground_window_elapsed", "background_foreground_window_active")
    if age <= timedelta(days=60):
        return (
            timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_10_TO_60_SECONDS),
            "background_10_60_ttl_elapsed",
            "background_10_60_ttl_active",
        )
    if age <= timedelta(days=180):
        return (
            timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_60_TO_180_SECONDS),
            "background_60_180_ttl_elapsed",
            "background_60_180_ttl_active",
        )
    if age <= timedelta(days=720):
        return (
            timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_180_TO_720_SECONDS),
            "background_180_720_ttl_elapsed",
            "background_180_720_ttl_active",
        )
    return (
        timedelta(seconds=EPISODE_RATINGS_BACKGROUND_BUCKET_720_PLUS_SECONDS),
        "background_720_plus_ttl_elapsed",
        "background_720_plus_ttl_active",
    )


def _artwork_refresh_due(
    asset_kind: str,
    *,
    status: str,
    last_checked_at: datetime | None,
    has_value: bool,
    trigger: str,
    first_aired: datetime | None,
    now: datetime,
) -> MetadataRefreshDecision:
    if trigger not in {TRIGGER_VIEWPORT, TRIGGER_PAGE_CONTEXT, TRIGGER_SYNC_EVENT}:
        return MetadataRefreshDecision(asset_kind, trigger, False, "trigger_not_supported")
    if status == ENRICH_STATUS_UNKNOWN:
        return MetadataRefreshDecision(asset_kind, trigger, True, "status_unknown")
    if status == ENRICH_STATUS_RETRYABLE_FAILURE:
        return _timed_refresh_decision(
            asset_kind,
            trigger,
            last_checked_at,
            timedelta(seconds=ARTWORK_RETRYABLE_FAILURE_BACKOFF_SECONDS),
            now,
            "retryable_failure_backoff_elapsed",
            "retryable_failure_backoff_active",
        )
    if status == ENRICH_STATUS_CHECKED_NO_DATA:
        if asset_kind == ASSET_KIND_STILL:
            ttl, due_reason, not_due_reason = _still_checked_no_data_ttl(trigger, first_aired, now)
            return _timed_refresh_decision(
                asset_kind,
                trigger,
                last_checked_at,
                ttl,
                now,
                due_reason,
                not_due_reason,
            )
        return _timed_refresh_decision(
            asset_kind,
            trigger,
            last_checked_at,
            timedelta(seconds=ARTWORK_EMPTY_REFRESH_SECONDS),
            now,
            "checked_no_data_ttl_elapsed",
            "checked_no_data_ttl_active",
        )
    if status == ENRICH_STATUS_READY or has_value:
        return MetadataRefreshDecision(asset_kind, trigger, False, "ready_artwork_not_rechecked")
    return MetadataRefreshDecision(asset_kind, trigger, True, "missing_value")


def _still_checked_no_data_ttl(
    trigger: str,
    first_aired: datetime | None,
    now: datetime,
) -> tuple[timedelta, str, str]:
    if _is_recently_released(first_aired, now):
        if trigger == TRIGGER_VIEWPORT:
            return (
                timedelta(seconds=STILL_VISIBLE_EMPTY_REFRESH_SECONDS),
                "checked_no_data_recent_visible_ttl_elapsed",
                "checked_no_data_recent_visible_ttl_active",
            )
        return (
            timedelta(seconds=STILL_RECENT_EMPTY_REFRESH_SECONDS),
            "checked_no_data_recent_ttl_elapsed",
            "checked_no_data_recent_ttl_active",
        )
    return (
        timedelta(seconds=ARTWORK_EMPTY_REFRESH_SECONDS),
        "checked_no_data_ttl_elapsed",
        "checked_no_data_ttl_active",
    )


def _is_recently_released(first_aired: datetime | None, now: datetime) -> bool:
    if first_aired is None:
        return False
    if first_aired > now:
        return False
    return now - first_aired <= timedelta(seconds=STILL_RECENT_RELEASE_WINDOW_SECONDS)


def _timed_refresh_decision(
    asset_kind: str,
    trigger: str,
    last_checked_at: datetime | None,
    ttl: timedelta,
    now: datetime,
    due_reason: str,
    not_due_reason: str,
) -> MetadataRefreshDecision:
    if last_checked_at is None:
        return MetadataRefreshDecision(asset_kind, trigger, True, "timestamp_missing")
    if now - last_checked_at >= ttl:
        return MetadataRefreshDecision(asset_kind, trigger, True, due_reason)
    return MetadataRefreshDecision(asset_kind, trigger, False, not_due_reason)
