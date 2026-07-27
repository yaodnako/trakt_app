from __future__ import annotations

import http.client
import json
import select
import socket
import ssl
import threading
from time import monotonic
from typing import Mapping
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen


_DOH_CACHE_TTL_SECONDS = 60.0
_doh_cache_guard = threading.Lock()
_doh_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_doh_host_locks: dict[str, threading.Lock] = {}


def resolve_ipv4_with_doh(host: str, *, timeout: float) -> list[str]:
    normalized_host = str(host or "").strip().casefold()
    with _doh_cache_guard:
        host_lock = _doh_host_locks.setdefault(normalized_host, threading.Lock())
    with host_lock:
        now = monotonic()
        with _doh_cache_guard:
            cached = _doh_cache.get(normalized_host)
        if cached is not None and cached[0] > now:
            return list(cached[1])

        query = urlencode({"name": normalized_host, "type": "A"})
        request = UrlRequest(
            f"https://1.1.1.1/dns-query?{query}",
            headers={"Accept": "application/dns-json"},
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        addresses: list[str] = []
        for answer in payload.get("Answer", []):
            if not isinstance(answer, dict) or answer.get("type") != 1:
                continue
            address = str(answer.get("data", "") or "")
            try:
                socket.inet_aton(address)
            except OSError:
                continue
            if address not in addresses:
                addresses.append(address)
        if addresses:
            with _doh_cache_guard:
                _doh_cache[normalized_host] = (
                    monotonic() + _DOH_CACHE_TTL_SECONDS,
                    tuple(addresses),
                )
        return addresses


def _invalidate_doh_cache(host: str, addresses: list[str]) -> None:
    normalized_host = str(host or "").strip().casefold()
    with _doh_cache_guard:
        cached = _doh_cache.get(normalized_host)
        if cached is not None and cached[1] == tuple(addresses):
            _doh_cache.pop(normalized_host, None)


def fragment_client_hello_records(data: bytes) -> bytes:
    if len(data) < 6 or data[0] != 22:
        return data
    record_size = int.from_bytes(data[3:5], "big")
    record_end = 5 + record_size
    if record_end > len(data):
        return data
    payload = data[5:record_end]
    if len(payload) < 2:
        return data
    version = data[1:3]
    fragments = (payload[:1], payload[1:40], payload[40:])
    reframed = b"".join(
        bytes((22,)) + version + len(fragment).to_bytes(2, "big") + fragment
        for fragment in fragments
        if fragment
    )
    return reframed + data[record_end:]


def request_with_fragmented_tls(
    host: str,
    target: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, str, http.client.HTTPMessage, bytes]:
    deadline = monotonic() + max(0.1, float(timeout))
    last_error: Exception | None = None
    addresses = resolve_ipv4_with_doh(host, timeout=min(2.0, _remaining_timeout(deadline)))
    for address in addresses:
        try:
            return _request_address(
                address,
                host=host,
                target=target,
                method=method,
                headers=headers or {},
                timeout=min(3.0, _remaining_timeout(deadline)),
            )
        except (OSError, ssl.SSLError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        _invalidate_doh_cache(host, addresses)
        raise last_error
    raise OSError(f"DNS-over-HTTPS lookup returned no IPv4 addresses for {host}")


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("fragmented TLS request budget exhausted")
    return remaining


def _request_address(
    address: str,
    *,
    host: str,
    target: str,
    method: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, str, http.client.HTTPMessage, bytes]:
    remote = socket.create_connection((address, 443), timeout=timeout)
    local, bridge = socket.socketpair()

    def relay() -> None:
        try:
            first_flight = bytearray()
            while len(first_flight) < 5 or len(first_flight) < 5 + int.from_bytes(first_flight[3:5], "big"):
                received = bridge.recv(65536)
                if not received:
                    return
                first_flight.extend(received)
            remote.sendall(fragment_client_hello_records(bytes(first_flight)))
            while True:
                readable, _, _ = select.select((bridge, remote), (), ())
                for source in readable:
                    received = source.recv(65536)
                    if not received:
                        return
                    (remote if source is bridge else bridge).sendall(received)
        except OSError:
            return
        finally:
            bridge.close()
            remote.close()

    relay_thread = threading.Thread(target=relay, name="fragmented-https", daemon=True)
    relay_thread.start()
    tls_socket = ssl.create_default_context().wrap_socket(local, server_hostname=host)
    try:
        tls_socket.settimeout(timeout)
        request_headers = {"Host": host, "Connection": "close", **headers}
        request_bytes = (
            f"{method.upper()} {target} HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
            + "\r\n"
        ).encode("utf-8")
        tls_socket.sendall(request_bytes)
        response = http.client.HTTPResponse(tls_socket)
        response.begin()
        body = response.read()
        return response.status, response.reason, response.headers, body
    finally:
        tls_socket.close()
        relay_thread.join(timeout=1.0)
