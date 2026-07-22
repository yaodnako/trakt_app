from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from trakt_tracker.infrastructure.trakt.client import TraktClient, TraktRateLimitError


def _client(handler, *, sleeps: list[float], budget: float = 300.0) -> TraktClient:
    client = TraktClient(
        "client",
        "secret",
        "http://127.0.0.1/callback",
        rate_limit_sleep=sleeps.append,
        rate_limit_jitter=lambda: 0.0,
        rate_limit_wait_budget_seconds=budget,
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_get_retries_after_numeric_retry_after(caplog) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, sleeps=sleeps)
    with caplog.at_level("WARNING"):
        result = client._request("GET", "/test?private=value", auth_required=False, use_cache=False)

    assert result == {"ok": True}
    assert sleeps == [2.0]
    assert len(calls) == 2
    assert "endpoint=/test" in caplog.text
    assert "private=value" not in caplog.text


def test_retry_after_supports_http_date() -> None:
    retry_at = format_datetime(datetime.now(tz=UTC) + timedelta(seconds=2), usegmt=True)
    parsed = TraktClient._retry_after_seconds(retry_at)
    assert parsed is not None
    assert 0 <= parsed <= 2


def test_get_does_not_wait_beyond_total_budget() -> None:
    sleeps: list[float] = []
    client = _client(lambda _request: httpx.Response(429, headers={"Retry-After": "301"}), sleeps=sleeps)

    with pytest.raises(TraktRateLimitError) as captured:
        client._request("GET", "/test", auth_required=False, use_cache=False)

    assert sleeps == []
    assert captured.value.retry_after_seconds == 301


def test_post_is_never_replayed_automatically() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "4"})

    client = _client(handler, sleeps=sleeps)
    with pytest.raises(TraktRateLimitError) as captured:
        client._request("POST", "/sync/history", auth_required=False, use_cache=False, json={})

    assert calls == 1
    assert sleeps == []
    assert captured.value.retry_after_seconds == 4
