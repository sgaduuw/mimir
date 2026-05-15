"""Canonical-inbox resolution for cross-posted articles.

A single message can land in multiple inboxes (lkml + linux-fsdevel,
say). Search engines treat the same content at multiple URLs as a
duplication signal and dilute ranking, so each article needs one
*canonical* URL, the one we tell search engines is the real source.

Strategy: read the author's intent from the RFC 5322 `To:` / `Cc:`
headers. The first list-shaped address there is what they actually
sent the message to; later ones are cross-post recipients. So if
`To: linux-fsdevel@vger.kernel.org` and `Cc: linux-kernel@...`,
linux-fsdevel is canonical.

To avoid false positives (people's personal addresses in Cc, vendor
auto-replies, etc.), "list-shaped" is a conservative suffix-based
filter, we only consider addresses on known mailing-list hosts.
Operator can extend via env if a host we don't yet know is in their
archive."""
from __future__ import annotations

from email.utils import getaddresses

# Hosts that overwhelmingly run mailing lists, not personal mailboxes.
# Used to filter To/Cc: only addresses on these hosts count when
# resolving canonical inbox or recording per-inbox address observations.
# Operator can extend via LIST_HOST_SUFFIXES env (comma-separated) once
# the setting is plumbed through; this baseline covers the kernel and
# adjacent projects archived on lore.kernel.org.
LIST_HOST_SUFFIXES: frozenset[str] = frozenset({
    # kernel.org-hosted (covers most lkml/* via vger relay)
    "vger.kernel.org",
    "lists.linux.dev",
    # NB: bare `kernel.org` is intentionally NOT here, it's primarily
    # a personal-address domain (gregkh@, torvalds@, cve@, etc.), so
    # including it produces false positives. List subdomains (vger,
    # subspace, lists.linux.dev) carry the actual list traffic.
    # graphics
    "lists.freedesktop.org",
    # arm, nvme, mtd, rdma, …
    "lists.infradead.org",
    # mm
    "kvack.org",
    # alsa
    "alsa-project.org",
    # powerpc, openbmc
    "lists.ozlabs.org",
    # linux foundation projects (cgroups, fsverity, etc.)
    "lists.linux-foundation.org",
    # linaro / arm ecosystem
    "lists.linaro.org",
    # qemu, gnu projects
    "nongnu.org",
    # ffmpeg
    "ffmpeg.org",
    # virtualization (libvirt, qemu, etc.)
    "redhat.com",
    # bitcoin, crypto-adjacent lists sometimes mirrored
    "lists.linux.it",
    # debian lists
    "lists.debian.org",
})


def is_list_address(address: str | None, suffixes: frozenset[str] = LIST_HOST_SUFFIXES) -> bool:
    """Conservative filter: True if `address` looks like a mailing-list
    address (i.e. its domain ends in a known list-host suffix). Empty,
    None, malformed, or off-list addresses return False."""
    if not address:
        return False
    at = address.rfind("@")
    if at < 0 or at == len(address) - 1:
        return False
    domain = address[at + 1:].strip().lower()
    if not domain:
        return False
    # Match exact host or any "<sub>.host" subdomain.
    return any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in suffixes
    )


def extract_list_addresses(headers: dict[str, str]) -> list[str]:
    """Pull list-shaped addresses out of `To:` then `Cc:`, preserving
    order. Returned addresses are lowercased and deduplicated; the
    first occurrence wins for ordering.

    Header keys are matched case-insensitively. RFC 5322 says field
    names are case-insensitive ("To" / "TO" / "to" are the same
    header); `mimir.parser` preserves whatever casing the wire
    delivered. Some ML re-mailers downcase headers, so a strict
    `headers.get("To")` silently returned `None` on legitimate input
    and broke both canonical-inbox resolution and the off-list-parent
    hint on the message page.
    """
    # Build a lowercased-key view of the headers we care about so
    # the walk can pick whichever casing the upstream sent.
    by_lower = {k.lower(): v for k, v in headers.items()}
    raw_values: list[str] = []
    for key in ("to", "cc"):
        value = by_lower.get(key)
        if value:
            raw_values.append(value)
    if not raw_values:
        return []

    seen: set[str] = set()
    out: list[str] = []
    # getaddresses parses a list of header values into [(name, address), ...].
    # Tolerates multiple addresses, RFC 5322 group syntax, malformed input.
    for _name, addr in getaddresses(raw_values):
        if not addr:
            continue
        normalized = addr.strip().lower()
        if normalized in seen:
            continue
        if not is_list_address(normalized):
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def pick_canonical_inbox_id(
    addresses: list[str],
    address_to_inbox_id: dict[str, int],
) -> int | None:
    """Walk `addresses` (already in To-then-Cc order) and return the
    inbox_id of the first address that maps to a known inbox. None if
    no address matches, caller falls back to the alphabetical-first
    rule at render time."""
    for addr in addresses:
        inbox_id = address_to_inbox_id.get(addr)
        if inbox_id is not None:
            return inbox_id
    return None
