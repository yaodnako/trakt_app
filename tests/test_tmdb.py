from __future__ import annotations

import ssl
import unittest
from email.message import Message
from unittest.mock import MagicMock
from unittest.mock import patch

import httpx

from trakt_tracker.infrastructure.fragmented_https import (
    _request_address,
    fragment_client_hello_records,
    request_with_fragmented_tls,
)
from trakt_tracker.infrastructure.tmdb import TMDbClient


class TMDbClientTests(unittest.TestCase):
    def test_tmdb_transport_chain_cannot_restart_the_request_budget(self) -> None:
        client = TMDbClient(read_access_token="token", timeout=15)
        client._cache = MagicMock()
        client._cache.get_json.return_value = None
        clock = [100.0]
        attempted: list[tuple[str, float]] = []

        def monotonic() -> float:
            return clock[0]

        def fail_direct(*_args, timeout: float, **_kwargs):
            attempted.append(("direct", timeout))
            clock[0] += timeout
            raise httpx.ConnectTimeout("direct failed")

        def fail_curl(*_args, timeout: float, **_kwargs):
            attempted.append(("curl", timeout))
            clock[0] += 4.0
            raise OSError("curl failed")

        def fail_fragmented(*_args, timeout: float, **_kwargs):
            attempted.append(("fragmented", timeout))
            clock[0] += timeout
            raise TimeoutError("fragmented TLS request budget exhausted")

        client._client = MagicMock()
        client._client.request.side_effect = fail_direct
        with (
            patch("trakt_tracker.infrastructure.tmdb.monotonic", side_effect=monotonic),
            patch("trakt_tracker.infrastructure.tmdb.subprocess.run", side_effect=fail_curl),
            patch("trakt_tracker.infrastructure.tmdb.request_with_fragmented_tls", side_effect=fail_fragmented),
            patch("trakt_tracker.infrastructure.tmdb.urlopen") as urllib_fallback,
        ):
            with self.assertRaisesRegex(TimeoutError, "budget exhausted"):
                client._request("GET", "/tv/999999/season/987")

        self.assertEqual(attempted, [("direct", 3.0), ("curl", 5.0), ("fragmented", 8.0)])
        self.assertEqual(clock[0], 115.0)
        urllib_fallback.assert_not_called()

    def test_tmdb_fallbacks_share_one_request_deadline(self) -> None:
        client = TMDbClient(read_access_token="token", timeout=15)
        client._cache = MagicMock()
        client._cache.get_json.return_value = None
        client._client = MagicMock()
        client._client.request.side_effect = httpx.ConnectTimeout("direct failed")
        payload = {"episodes": []}

        with (
            patch("trakt_tracker.infrastructure.tmdb.monotonic", return_value=100.0),
            patch.object(client, "_request_with_curl", return_value=payload) as fallback,
        ):
            result = client._request("GET", "/tv/999999/season/987")

        self.assertEqual(result, payload)
        self.assertEqual(client._client.request.call_args.kwargs["timeout"], 3.0)
        self.assertEqual(fallback.call_args.kwargs["deadline"], 115.0)

    def test_fragmented_tls_addresses_share_one_timeout_budget(self) -> None:
        clock = [0.0]
        attempt_timeouts: list[float] = []

        def monotonic() -> float:
            return clock[0]

        def fail_address(*_args, timeout: float, **_kwargs):
            attempt_timeouts.append(timeout)
            clock[0] += timeout
            raise TimeoutError("address timed out")

        with (
            patch("trakt_tracker.infrastructure.fragmented_https.monotonic", side_effect=monotonic),
            patch(
                "trakt_tracker.infrastructure.fragmented_https.resolve_ipv4_with_doh",
                return_value=["203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4"],
            ),
            patch("trakt_tracker.infrastructure.fragmented_https._request_address", side_effect=fail_address),
        ):
            with self.assertRaisesRegex(TimeoutError, "budget exhausted"):
                request_with_fragmented_tls("api.themoviedb.org", "/3/tv/1/season/1", timeout=5)

        self.assertEqual(attempt_timeouts, [3.0, 2.0])
        self.assertLessEqual(sum(attempt_timeouts), 5.0)

    def test_fragment_client_hello_records_preserves_handshake_payload(self) -> None:
        payload = bytes(range(100))
        original = b"\x16\x03\x01" + len(payload).to_bytes(2, "big") + payload

        fragmented = fragment_client_hello_records(original)

        restored = bytearray()
        offset = 0
        record_count = 0
        while offset < len(fragmented):
            self.assertEqual(fragmented[offset], 22)
            size = int.from_bytes(fragmented[offset + 3 : offset + 5], "big")
            restored.extend(fragmented[offset + 5 : offset + 5 + size])
            offset += 5 + size
            record_count += 1
        self.assertEqual(bytes(restored), payload)
        self.assertEqual(record_count, 3)

    def test_curl_failure_uses_fragmented_tls_before_plain_dns_fallback(self) -> None:
        client = TMDbClient(read_access_token="token")
        payload = {"episodes": []}

        with (
            patch("trakt_tracker.infrastructure.tmdb.subprocess.run", side_effect=OSError("curl failed")),
            patch.object(client, "_request_with_fragmented_tls", return_value=payload) as fragmented,
            patch.object(client, "_request_with_urllib") as urllib_fallback,
        ):
            result = client._request_with_curl("GET", "/tv/1/season/1", headers={"Accept": "application/json"}, params={})

        self.assertEqual(result, payload)
        fragmented.assert_called_once()
        urllib_fallback.assert_not_called()

    def test_fragmented_tls_error_is_preserved_if_plain_dns_fallback_also_fails(self) -> None:
        client = TMDbClient(read_access_token="token")
        tls_error = ssl.SSLError("fragmented TLS failed")

        with (
            patch("trakt_tracker.infrastructure.tmdb.subprocess.run", side_effect=OSError("curl failed")),
            patch.object(client, "_request_with_fragmented_tls", side_effect=tls_error),
            patch.object(client, "_request_with_urllib", side_effect=OSError("plain DNS failed")),
        ):
            with self.assertRaisesRegex(ssl.SSLError, "fragmented TLS failed"):
                client._request_with_curl("GET", "/tv/1/season/1", headers={"Accept": "application/json"}, params={})

    def test_fragmented_transport_empty_http_read_returns_without_busy_loop(self) -> None:
        remote = MagicMock()
        local = MagicMock()
        bridge = MagicMock()
        tls_socket = MagicMock()
        context = MagicMock()
        context.wrap_socket.return_value = tls_socket
        response = MagicMock()
        response.status = 200
        response.reason = "OK"
        response.headers = Message()
        response.read.return_value = b""
        relay_thread = MagicMock()

        with (
            patch("trakt_tracker.infrastructure.fragmented_https.socket.create_connection", return_value=remote),
            patch("trakt_tracker.infrastructure.fragmented_https.socket.socketpair", return_value=(local, bridge)),
            patch("trakt_tracker.infrastructure.fragmented_https.ssl.create_default_context", return_value=context),
            patch("trakt_tracker.infrastructure.fragmented_https.http.client.HTTPResponse", return_value=response),
            patch("trakt_tracker.infrastructure.fragmented_https.threading.Thread", return_value=relay_thread),
        ):
            status, reason, _headers, body = _request_address(
                "203.0.113.1",
                host="api.themoviedb.org",
                target="/3/tv/1/season/1",
                method="GET",
                headers={"Accept": "application/json"},
                timeout=1,
            )

        self.assertEqual((status, reason, body), (200, "OK", b""))
        response.read.assert_called_once_with()
        relay_thread.join.assert_called_once_with(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
