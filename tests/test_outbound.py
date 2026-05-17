"""Tests for `mimir._outbound`.

The validator's job is to reduce SSRF surface on operator-supplied
URLs; the no-redirect handler's job is to keep `urllib.request`
from silently following 3xx into internal hosts (or, on POSTs that
carry secrets, exfiltrating the body).

These are pure unit tests, no network, no Settings fixture.
"""
import http.client
import io
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from mimir._outbound import (
    OUTBOUND_OPENER,
    NoRedirectHandler,
    OutboundUrlError,
    validate_outbound_url,
)


# --- validate_outbound_url: happy paths ---


def test_accepts_https_url():
    assert (
        validate_outbound_url("https://lore.kernel.org/lkml")
        == "https://lore.kernel.org/lkml"
    )


def test_strips_surrounding_whitespace():
    assert (
        validate_outbound_url("  https://lore.kernel.org/lkml  ")
        == "https://lore.kernel.org/lkml"
    )


def test_accepts_http_when_allow_http_set():
    assert (
        validate_outbound_url("http://example.com", allow_http=True)
        == "http://example.com"
    )


def test_accepts_public_ipv4_literal():
    """Public routable IPs (e.g. 1.1.1.1) are allowed. Operator might
    pin to one to avoid DNS, that's a legitimate choice."""
    assert (
        validate_outbound_url("https://1.1.1.1/manifest.js.gz")
        == "https://1.1.1.1/manifest.js.gz"
    )


def test_accepts_public_ipv6_literal():
    """Public routable IPv6 ditto. 2606:4700:4700::1111 is Cloudflare's
    public resolver."""
    assert (
        validate_outbound_url("https://[2606:4700:4700::1111]/")
        == "https://[2606:4700:4700::1111]/"
    )


# --- validate_outbound_url: input-shape rejections ---


def test_rejects_non_string():
    with pytest.raises(OutboundUrlError, match="must be a string"):
        validate_outbound_url(b"https://lore.kernel.org/lkml")  # type: ignore[arg-type]


def test_rejects_empty_string():
    with pytest.raises(OutboundUrlError, match="must not be empty"):
        validate_outbound_url("")


def test_rejects_whitespace_only_string():
    with pytest.raises(OutboundUrlError, match="must not be empty"):
        validate_outbound_url("   ")


# --- validate_outbound_url: scheme rejections ---


def test_rejects_http_by_default():
    with pytest.raises(OutboundUrlError, match="scheme"):
        validate_outbound_url("http://example.com")


@pytest.mark.parametrize("scheme", ["file", "git", "ftp", "data", "javascript"])
def test_rejects_non_https_schemes(scheme):
    """The validator's allowlist is `https` only (or `http` opt-in).
    Anything else is rejected, regardless of how `urllib` would
    handle it; this includes `file://` (urllib reads local files),
    `git://` (no transport security), and `data:` (no host at all)."""
    with pytest.raises(OutboundUrlError, match="scheme"):
        validate_outbound_url(f"{scheme}://example.com")


def test_rejects_non_http_scheme_even_when_allow_http_set():
    """`allow_http=True` widens to `http`, not to every scheme."""
    with pytest.raises(OutboundUrlError, match="scheme"):
        validate_outbound_url("file:///etc/passwd", allow_http=True)


# --- validate_outbound_url: host rejections ---


def test_rejects_url_without_host():
    with pytest.raises(OutboundUrlError, match="host"):
        validate_outbound_url("https://")


@pytest.mark.parametrize("host", ["localhost", "LocalHost", "localhost.localdomain"])
def test_rejects_localhost_hostnames(host):
    """The literal hostnames `localhost` / `localhost.localdomain` are
    in the deny list; matching is case-insensitive."""
    with pytest.raises(OutboundUrlError, match="local"):
        validate_outbound_url(f"https://{host}/x")


# --- validate_outbound_url: IP-literal rejections ---


@pytest.mark.parametrize(
    "ip,reason",
    [
        ("127.0.0.1", "loopback"),
        ("127.255.255.254", "loopback"),
        ("169.254.169.254", "AWS/GCP/Azure metadata, link-local"),
        ("169.254.1.1", "link-local"),
        ("10.0.0.1", "RFC 1918"),
        ("172.16.0.1", "RFC 1918"),
        ("172.31.255.255", "RFC 1918"),
        ("192.168.1.1", "RFC 1918"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("240.0.0.1", "reserved"),
        ("255.255.255.255", "broadcast/reserved"),
    ],
)
def test_rejects_private_ipv4_literals(ip, reason):
    with pytest.raises(OutboundUrlError, match="non-routable"):
        validate_outbound_url(f"https://{ip}/x"), reason


@pytest.mark.parametrize(
    "ip,reason",
    [
        ("::1", "IPv6 loopback"),
        ("fe80::1", "IPv6 link-local"),
        ("fc00::1", "IPv6 ULA"),
        ("fd00::1", "IPv6 ULA"),
        ("::", "IPv6 unspecified"),
        ("ff02::1", "IPv6 multicast"),
    ],
)
def test_rejects_private_ipv6_literals(ip, reason):
    with pytest.raises(OutboundUrlError, match="non-routable"):
        validate_outbound_url(f"https://[{ip}]/x"), reason


def test_rejects_ipv4_mapped_ipv6_loopback():
    """`::ffff:127.0.0.1` is an IPv4-mapped IPv6 address that the
    socket layer routes to 127.0.0.1. The validator must unwrap and
    re-check against the IPv4 rules, otherwise `::ffff:127.0.0.1`
    is a trivial bypass of the IPv4 deny list."""
    with pytest.raises(OutboundUrlError, match="non-routable"):
        validate_outbound_url("https://[::ffff:127.0.0.1]/x")


# --- NoRedirectHandler ---


def _fake_3xx_response(code: int):
    """Build the (req, fp, code, msg, headers) tuple the handler
    expects, without needing a real HTTP server."""
    req = Request("https://upstream.example/lkml/manifest.js.gz")
    fp = io.BytesIO(b"")
    msg = "Found"
    headers = http.client.HTTPMessage()
    headers["Location"] = "https://attacker.example/exfil"
    return req, fp, code, msg, headers


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_no_redirect_handler_raises_on_any_3xx(code):
    """Each of the redirect statuses raises rather than chasing the
    Location header. Without this the IndexNow POST body (carrying
    the operator's key) would follow a 302 to an attacker host."""
    handler = NoRedirectHandler()
    req, fp, _code, msg, headers = _fake_3xx_response(code)
    method = getattr(handler, f"http_error_{code}")
    with pytest.raises(HTTPError) as ei:
        method(req, fp, code, msg, headers)
    assert ei.value.code == code
    assert "redirect-following disabled" in str(ei.value)


def test_outbound_opener_has_no_redirect_handler():
    """The module-level `OUTBOUND_OPENER` singleton is built with
    `NoRedirectHandler` installed. Other call sites that import it
    inherit the hardened posture automatically; nothing else needs
    to remember to install it."""
    handlers = [h for h in OUTBOUND_OPENER.handlers if isinstance(h, NoRedirectHandler)]
    assert len(handlers) == 1
