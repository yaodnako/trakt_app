from __future__ import annotations

import ssl
import unittest
from email.message import Message
from threading import Event, Thread
from unittest.mock import MagicMock
from unittest.mock import patch

import httpx

from trakt_tracker.infrastructure.fragmented_https import (
    _request_address,
    fragment_client_hello_records,
    request_with_fragmented_tls,
    resolve_ipv4_with_doh,
)
from trakt_tracker.infrastructure.tmdb import TMDbClient


class TMDbClientTests(unittest.TestCase):
    def test_loopback_only_system_dns_uses_fragmented_transport_first(self) -> None:
        client = TMDbClient(read_access_token="token")
        client._cache = MagicMock()
        client._cache.get_json.return_value = None
        direct_response = MagicMock()
        direct_response.json.return_value = {"transport": "direct"}
        client._client = MagicMock()
        client._client.request.return_value = direct_response
        fragmented_payload = {"transport": "fragmented"}

        with (
            patch(
                "socket.getaddrinfo",
                return_value=[
                    (2, 1, 6, "", ("127.0.0.1", 443)),
                    (23, 1, 6, "", ("::1", 443, 0, 0)),
                ],
            ) as resolve,
            patch.object(
                client,
                "_request_with_fragmented_tls",
                return_value=fragmented_payload,
            ) as fragmented,
            patch.object(client, "_request_with_curl") as curl_fallback,
        ):
            first = client._request("GET", "/tv/1/season/1")
            second = client._request("GET", "/tv/2/season/1")

        self.assertEqual(first, fragmented_payload)
        self.assertEqual(second, fragmented_payload)
        resolve.assert_called_once()
        self.assertEqual(fragmented.call_count, 2)
        client._client.request.assert_not_called()
        curl_fallback.assert_not_called()

    def test_public_system_dns_keeps_pooled_direct_transport(self) -> None:
        client = TMDbClient(read_access_token="token")
        client._cache = MagicMock()
        client._cache.get_json.return_value = None
        direct_response = MagicMock()
        direct_response.json.return_value = {"transport": "direct"}
        client._client = MagicMock()
        client._client.request.return_value = direct_response

        with (
            patch(
                "socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
            ) as resolve,
            patch.object(client, "_request_with_fragmented_tls") as fragmented,
        ):
            first = client._request("GET", "/tv/1/season/1")
            second = client._request("GET", "/tv/2/season/1")

        self.assertEqual(first, {"transport": "direct"})
        self.assertEqual(second, {"transport": "direct"})
        resolve.assert_called_once()
        self.assertEqual(client._client.request.call_count, 2)
        fragmented.assert_not_called()

    def test_loopback_dns_fragmented_failure_keeps_existing_fallback_chain(self) -> None:
        client = TMDbClient(read_access_token="token")
        client._direct_dns_loopback_only = True
        client._cache = MagicMock()
        client._cache.get_json.return_value = None
        payload = {"transport": "curl"}

        with (
            patch.object(
                client,
                "_request_with_fragmented_tls",
                side_effect=OSError("fragmented failed"),
            ) as fragmented,
            patch.object(client, "_request_with_curl", return_value=payload) as curl_fallback,
        ):
            result = client._request("GET", "/tv/1/season/1")

        self.assertEqual(result, payload)
        fragmented.assert_called_once()
        curl_fallback.assert_called_once()

    def test_tmdb_transport_chain_cannot_restart_the_request_budget(self) -> None:
        client = TMDbClient(read_access_token="token", timeout=15)
        client._direct_dns_loopback_only = False
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
        client._direct_dns_loopback_only = False
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

    def test_doh_resolution_reuses_recent_host_addresses(self) -> None:
        response = MagicMock()
        response.read.return_value = (
            b'{"Answer":[{"type":1,"data":"203.0.113.10"},'
            b'{"type":1,"data":"203.0.113.11"}]}'
        )
        context = MagicMock()
        context.__enter__.return_value = response

        with patch(
            "trakt_tracker.infrastructure.fragmented_https.urlopen",
            return_value=context,
        ) as request:
            first = resolve_ipv4_with_doh("cache-test.invalid", timeout=1)
            second = resolve_ipv4_with_doh("cache-test.invalid", timeout=1)

        self.assertEqual(first, ["203.0.113.10", "203.0.113.11"])
        self.assertEqual(second, first)
        request.assert_called_once()

    def test_concurrent_doh_resolution_uses_one_host_lookup(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"Answer":[{"type":1,"data":"203.0.113.20"}]}'
        context = MagicMock()
        context.__enter__.return_value = response
        started = Event()
        release = Event()

        def resolve(*_args, **_kwargs):
            started.set()
            release.wait(timeout=1)
            return context

        results: list[list[str]] = []
        workers = [
            Thread(
                target=lambda: results.append(
                    resolve_ipv4_with_doh("singleflight-test.invalid", timeout=1)
                )
            )
            for _ in range(4)
        ]
        with patch(
            "trakt_tracker.infrastructure.fragmented_https.urlopen",
            side_effect=resolve,
        ) as request:
            workers[0].start()
            try:
                self.assertTrue(started.wait(timeout=1))
                for worker in workers[1:]:
                    worker.start()
                Event().wait(0.05)
                request.assert_called_once()
            finally:
                release.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(timeout=1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(results, [["203.0.113.20"]] * 4)
        request.assert_called_once()

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
