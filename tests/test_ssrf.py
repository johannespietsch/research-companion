"""Tests for the SSRF guard in bot.ssrf.

DNS resolution is mocked so these run offline. `_resolve_ips` is the single
seam: we point it at whatever address(es) a hostname should "resolve" to.
"""
from __future__ import annotations

import pytest

from bot import ssrf
from bot.ssrf import BlockedURLError, assert_public_url, is_public_url


@pytest.fixture
def resolve_to(monkeypatch):
    """Make every hostname resolve to the given list of IPs."""
    def _set(*ips: str):
        monkeypatch.setattr(ssrf, "_resolve_ips", lambda host: list(ips))
    return _set


class TestAllowed:
    def test_public_https_passes(self, resolve_to):
        resolve_to("93.184.216.34")
        assert_public_url("https://example.com/article")  # no raise
        assert is_public_url("https://example.com/article")

    def test_public_http_with_explicit_port_80(self, resolve_to):
        resolve_to("93.184.216.34")
        assert_public_url("http://example.com:80/x")

    def test_public_ipv6_passes(self, resolve_to):
        resolve_to("2606:2800:220:1:248:1893:25c8:1946")
        assert_public_url("https://example.com/")


class TestBlockedHostnames:
    @pytest.mark.parametrize("url", [
        "http://localhost/x",
        "http://LOCALHOST:80/",
        "https://foo.internal/x",
        "https://db.local/x",
        "https://service.localhost/",
    ])
    def test_internal_hostnames_blocked(self, url, resolve_to):
        resolve_to("93.184.216.34")  # even if DNS would say public
        assert not is_public_url(url)
        with pytest.raises(BlockedURLError):
            assert_public_url(url)


class TestBlockedSchemesAndPorts:
    @pytest.mark.parametrize("url", [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/plain,hi",
    ])
    def test_non_http_schemes_blocked(self, url):
        assert not is_public_url(url)

    def test_nonstandard_port_blocked(self, resolve_to):
        resolve_to("93.184.216.34")
        with pytest.raises(BlockedURLError):
            assert_public_url("http://example.com:8080/admin")


class TestNonPublicAddresses:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1",        # loopback
        "10.0.0.5",         # private
        "192.168.1.10",     # private
        "172.16.0.1",       # private
        "169.254.169.254",  # link-local (cloud metadata)
        "::1",              # ipv6 loopback
        "fc00::1",          # ipv6 unique-local
        "fe80::1",          # ipv6 link-local
        "::ffff:127.0.0.1", # ipv4-mapped loopback
        "0.0.0.0",          # unspecified
    ])
    def test_literal_private_ip_blocked(self, ip, resolve_to):
        resolve_to(ip)
        with pytest.raises(BlockedURLError):
            assert_public_url(f"http://{ip}/")

    def test_dns_rebinding_blocked(self, resolve_to):
        # Hostname looks innocent but resolves to a private address.
        resolve_to("10.1.2.3")
        with pytest.raises(BlockedURLError):
            assert_public_url("https://totally-legit.example/")

    def test_mixed_answers_blocked_if_any_private(self, resolve_to):
        # A single private answer is enough to refuse the whole request.
        resolve_to("93.184.216.34", "10.0.0.1")
        with pytest.raises(BlockedURLError):
            assert_public_url("https://example.com/")


class TestResolutionFailures:
    def test_unresolvable_host_blocked(self, monkeypatch):
        import socket
        def _boom(host):
            raise socket.gaierror("nope")
        monkeypatch.setattr(ssrf, "_resolve_ips", _boom)
        with pytest.raises(BlockedURLError):
            assert_public_url("https://does-not-exist.example/")

    def test_missing_host_blocked(self):
        with pytest.raises(BlockedURLError):
            assert_public_url("https:///path-only")
