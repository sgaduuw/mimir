"""Shared outbound-HTTP infrastructure.

Two callers use this module:

- `mimir.sync.fetch_manifest` for the public-inbox `manifest.js.gz`
  GET, configured via per-inbox `upstream_url`.
- `mimir.indexnow.notify` for the IndexNow POST, configured via
  `Settings.indexnow_endpoint`.

And three validators delegate to it:

- `mimir.inboxes.validate_upstream_url` (admin CLI for
  `Inbox.upstream_url`).
- pydantic `field_validator` on `Settings.mainline_tree_url`.
- pydantic `field_validator` on `Settings.indexnow_endpoint`.

The shape is three pieces working together:

- `validate_outbound_url(url, *, allow_http=False)` for the
  scheme + host + IP-literal checks. Reduces SSRF surface when
  operator-controlled URL config crosses any boundary that is
  less trusted than the operator (today: CLI-only; tomorrow: the
  deferred Flask admin UI). Rejecting `127.0.0.1`,
  `169.254.169.254` (AWS/GCP/Azure metadata),
  RFC 1918 / link-local / IPv6 ULA / loopback also keeps a
  fat-fingered operator from accidentally pointing at internal
  services and getting confusing errors.
- `NoRedirectHandler` for `urllib.request`. The stdlib default
  follows up to 10 redirects across hosts and (downgrading)
  schemes with no per-hop callback, so a compromised upstream
  can bounce a fetch into internal-only URLs. For POST endpoints
  that carry secrets in the body (IndexNow's key is the worst
  example), the same redirect path also exfiltrates the body.
  We raise on any 3xx instead.
- `OUTBOUND_OPENER` builds the opener once, with the no-redirect
  handler installed. Callers use `OUTBOUND_OPENER.open(req,
  timeout=...)` in place of `urllib.request.urlopen(...)`. Tests
  patch `OUTBOUND_OPENER.open` to fake the network.

Known gap (deliberate, not a bug): DNS rebinding. A hostname can
pass `validate_outbound_url` (its A record points at a routable
IP) and then resolve to `127.0.0.1` at fetch time. Mitigating
that needs a custom resolver that pins the IP from validate-time
and re-uses it at fetch-time; more weight than the current
threat model justifies. Revisit if the admin UI ships and the
trust boundary moves outward.
"""

import ipaddress
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener


# Hostnames that aren't IP literals but unambiguously target the
# local machine. Anything else that resolves locally is caught by
# the IP-literal check after `gethostbyname`, except we deliberately
# don't resolve (DNS rebinding caveat above), so the deny list here
# is a small explicit set.
_DENY_HOSTS = frozenset({"localhost", "localhost.localdomain"})


class OutboundUrlError(ValueError):
    """Raised by `validate_outbound_url` on invalid input. Message
    is operator-facing; callers that have their own error type
    (e.g. `InboxValidationError`) should wrap and re-raise."""


def validate_outbound_url(url: str, *, allow_http: bool = False) -> str:
    """Validate `url` as a safe target for an operator-supplied
    outbound fetch. Returns the stripped URL on success; raises
    `OutboundUrlError` on any rejection.

    Rejects:
    - Non-string / empty / whitespace-only input.
    - Schemes other than `https` (or `http` when `allow_http=True`).
      Refuses `file://`, `git://`, `data:`, anything else.
    - Empty / missing host.
    - Hostnames `localhost` / `localhost.localdomain`.
    - IP literals in any non-public range: loopback (127.0.0.0/8,
      ::1), link-local (169.254.0.0/16 incl. cloud-metadata
      169.254.169.254; fe80::/10), RFC 1918 (10/8, 172.16/12,
      192.168/16) and IPv6 ULA (fc00::/7), unspecified (0.0.0.0,
      ::), multicast, reserved. IPv4-mapped IPv6 addresses
      (`::ffff:127.0.0.1`) are unwrapped and re-checked against
      the IPv4 rules.

    Public IP literals (e.g. `1.1.1.1`) and public hostnames pass.

    Does NOT resolve hostnames. See module docstring for the DNS-
    rebinding caveat that follows from that choice.
    """
    if not isinstance(url, str):
        raise OutboundUrlError("url must be a string")
    url = url.strip()
    if not url:
        raise OutboundUrlError("url must not be empty")
    parsed = urlparse(url)
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in allowed_schemes:
        joined = " or ".join(repr(s) for s in allowed_schemes)
        raise OutboundUrlError(f"url scheme must be {joined} (got {parsed.scheme!r})")
    host = parsed.hostname
    if not host:
        raise OutboundUrlError("url must include a host")
    if host.lower() in _DENY_HOSTS:
        raise OutboundUrlError(f"url host {host!r} targets the local machine")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal, it's a hostname. Subject to the
        # DNS-rebinding caveat in the module docstring; we accept.
        return url
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise OutboundUrlError(
            f"url host {host!r} is a non-routable / private-range IP"
        )
    return url


class NoRedirectHandler(HTTPRedirectHandler):
    """`urllib.request` handler that refuses to follow any 3xx.

    Replaces the stdlib default's silent multi-hop redirect chain
    (up to 10 hops, no per-target inspection, scheme downgrades
    allowed). Raises `HTTPError` on the first 3xx so the calling
    code surfaces the rejection rather than silently fetching the
    redirect target.
    """

    def http_error_301(self, req, fp, code, msg, headers):
        raise HTTPError(
            req.full_url,
            code,
            f"redirect-following disabled (HTTP {code})",
            headers,
            fp,
        )

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


OUTBOUND_OPENER = build_opener(NoRedirectHandler())


__all__ = [
    "NoRedirectHandler",
    "OUTBOUND_OPENER",
    "OutboundUrlError",
    "validate_outbound_url",
]
