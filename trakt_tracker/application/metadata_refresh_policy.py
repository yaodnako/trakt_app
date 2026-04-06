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
ASSET_KIND_STILL = "still"

TRIGGER_VIEWPORT = "viewport"
TRIGGER_VISIBLE_RATINGS_REFRESH = "visible_ratings_refresh"
TRIGGER_SYNC_EVENT = "sync_event"
TRIGGER_MANUAL_REPAIR = "manual_repair"

TITLE_ALLOWED_PARTS = (ASSET_KIND_POSTER, ASSET_KIND_TITLE_RATINGS)
EPISODE_ALLOWED_PARTS = (ASSET_KIND_STILL, ASSET_KIND_EPISODE_RATINGS)

TITLE_RATINGS_READY_REFRESH_SECONDS = 300
EPISODE_RATINGS_READY_REFRESH_SECONDS = 300
RATINGS_EMPTY_REFRESH_SECONDS = 21600
RATINGS_RETRYABLE_FAILURE_BACKOFF_SECONDS = 1800
ARTWORK_EMPTY_REFRESH_SECONDS = 604800
ARTWORK_RETRYABLE_FAILURE_BACKOFF_SECONDS = 21600


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
        TRIGGER_VISIBLE_RATINGS_REFRESH,
        TRIGGER_SYNC_EVENT,
        TRIGGER_MANUAL_REPAIR,
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
            now=now_value,
        )
    if asset_kind in {ASSET_KIND_POSTER, ASSET_KIND_STILL}:
        return _artwork_refresh_due(
            asset_kind,
            status=normalized_status,
            last_checked_at=checked_at,
            has_value=has_value,
            trigger=normalized_trigger,
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
    now: datetime,
) -> MetadataRefreshDecision:
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


def _artwork_refresh_due(
    asset_kind: str,
    *,
    status: str,
    last_checked_at: datetime | None,
    has_value: bool,
    trigger: str,
    now: datetime,
) -> MetadataRefreshDecision:
    if trigger not in {TRIGGER_VIEWPORT, TRIGGER_SYNC_EVENT}:
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
