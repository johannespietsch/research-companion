"""SSRF guard: refuse to fetch URLs that resolve to non-public addresses.

User-submitted URLs are fetched server-side (the article / PDF / video / tweet
paths in `fetcher.py`), so without this a user could point us at localhost, a
link-local metadata endpoint, or — on Fly — the private 6PN `.internal`
network and reach other apps in the org.

The guard resolves the host up front and rejects the request if *any* resolved
address is private/loopback/link-local/reserved (a single bad answer is enough,
which also defends against split-horizon DNS). It is deliberately strict: only
http/https on the standard ports, and no obviously-internal hostnames.

Known limitation: this validates the *initial* target. A server that returns a
3xx redirect to an internal host could still bypass it via the underlying HTTP
clients' auto-redirect following. Redirect pinning across httpx / curl_cffi /
yt-dlp is a larger change tracked as a follow-up; `assert_response_url_public`
gives a cheap post-fetch re-check for the clients that expose the final URL.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}
# Standard web ports only. `None` = no explicit port (i.e. the scheme default).
_ALLOWED_PORTS = {None, 80, 443}
# Hostnames that should never be fetched even before DNS resolution.
_BLOCKED_HOSTNAMES = {"localhost"}
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost")


class BlockedURLError(Exception):
    """Raised when a URL is not safe to fetch (bad scheme/port or non-public host)."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) would otherwise sneak past the v4
    # checks — collapse it to the embedded v4 address first.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve_ips(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def assert_public_url(url: str) -> None:
    """Raise BlockedURLError unless `url` is a public http(s) URL on a safe port.

    Resolves the host and requires every returned address to be public.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"scheme not allowed: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise BlockedURLError("missing host")
    host_l = host.lower().rstrip(".")
    if host_l in _BLOCKED_HOSTNAMES or host_l.endswith(_BLOCKED_SUFFIXES):
        raise BlockedURLError(f"host not allowed: {host!r}")

    try:
        port = parsed.port
    except ValueError as e:  # malformed port, e.g. http://h:99999/
        raise BlockedURLError("invalid port") from e
    if port not in _ALLOWED_PORTS:
        raise BlockedURLError(f"port not allowed: {port}")

    try:
        ips = _resolve_ips(host)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURLError(f"could not resolve host: {host!r}") from e
    if not ips:
        raise BlockedURLError(f"no addresses for host: {host!r}")
    for ip in ips:
        if not _ip_is_public(ip):
            raise BlockedURLError(f"non-public address for {host!r}: {ip}")


def is_public_url(url: str) -> bool:
    """Boolean convenience wrapper around `assert_public_url`."""
    try:
        assert_public_url(url)
        return True
    except BlockedURLError:
        return False
