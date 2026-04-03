from __future__ import annotations


ENRICH_STATUS_UNKNOWN = "unknown"
ENRICH_STATUS_READY = "ready"
ENRICH_STATUS_CHECKED_NO_DATA = "checked_no_data"
ENRICH_STATUS_RETRYABLE_FAILURE = "retryable_failure"

TERMINAL_ENRICH_STATUSES = {
    ENRICH_STATUS_READY,
    ENRICH_STATUS_CHECKED_NO_DATA,
}


def is_terminal_enrich_status(status: str | None) -> bool:
    return (status or ENRICH_STATUS_UNKNOWN) in TERMINAL_ENRICH_STATUSES


def should_attempt_enrich(status: str | None, *, has_value: bool = False) -> bool:
    normalized = status or ENRICH_STATUS_UNKNOWN
    if has_value:
        return False
    return normalized not in TERMINAL_ENRICH_STATUSES
