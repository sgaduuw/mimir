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

import re
from email.utils import getaddresses

# Hosts that overwhelmingly run mailing lists, not personal mailboxes.
# Used to filter To/Cc: only addresses on these hosts count when
# resolving canonical inbox or recording per-inbox address observations.
# Operators extend via `Settings.list_host_suffix_overrides` (env
# `LIST_HOST_SUFFIX_OVERRIDES`, comma-separated); the effective set
# returned by `_effective_suffixes()` is the union of this baseline
# and the operator additions.
LIST_HOST_SUFFIXES: frozenset[str] = frozenset(
    {
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
    }
)


def _effective_suffixes() -> frozenset[str]:
    """Return the operator-extended suffix set. Reads
    `Settings.list_host_suffix_overrides` lazily so monkeypatched
    settings in tests are honored. The baseline `LIST_HOST_SUFFIXES`
    is always included."""
    # Late import: avoids a circular dep through `mimir.config` (which
    # imports models which import canonical for is_list_address).
    from mimir.config import settings

    overrides = settings.list_host_suffix_overrides or []
    if not overrides:
        return LIST_HOST_SUFFIXES
    return LIST_HOST_SUFFIXES | {s.strip().lower() for s in overrides if s.strip()}


def is_list_address(
    address: str | None,
    suffixes: frozenset[str] | None = None,
) -> bool:
    """Conservative filter: True if `address` looks like a mailing-list
    address (i.e. its domain ends in a known list-host suffix). Empty,
    None, malformed, or off-list addresses return False.

    `suffixes=None` (the default) reads the effective set from
    settings on each call. Tests / callers can pass an explicit set
    to bypass settings."""
    if not address:
        return False
    at = address.rfind("@")
    if at < 0 or at == len(address) - 1:
        return False
    domain = address[at + 1 :].strip().lower()
    if not domain:
        return False
    active = suffixes if suffixes is not None else _effective_suffixes()
    # Match exact host or any "<sub>.host" subdomain.
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in active)


# Strips C0/C1 control bytes and surrogate-range codepoints from an
# address before it flows into HTML attribute contexts. Jinja
# autoescape already handles `<`, `>`, `"`, `&` for us, but a
# surrogate-escaped byte in the raw header (which `parsed.headers`
# carries verbatim, untouched by the surrogate scrub on parsed
# *fields*) would survive autoescape and break the rendered attribute.
_UNSAFE_ATTR_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


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
        # Strip control bytes and surrogate codepoints before any
        # further processing. The raw header `parsed.headers["To"]`
        # carries unscrubbed bytes (surrogate scrub runs on
        # ParsedArticle's typed fields, not on the headers dict);
        # an address that survives this filter is safe to splice
        # into an HTML attribute under Jinja's autoescape.
        cleaned = _UNSAFE_ATTR_CHARS_RE.sub("", addr).strip()
        if not cleaned:
            continue
        # Cap per-address length so a pathological multi-KB
        # localpart can't bloat the rendered tooltip beyond what a
        # real reader sees in a few hundred chars. Real list
        # addresses are well under 100 chars.
        if len(cleaned) > 254:  # RFC 5321 path length cap
            cleaned = cleaned[:254]
        normalized = cleaned.lower()
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
    demoted_inbox_ids: frozenset[int] = frozenset(),
) -> int | None:
    """Walk `addresses` (already in To-then-Cc order) and return the
    inbox_id of the best matching inbox. None if no address matches;
    caller falls back to the alphabetical-first rule at render time.

    Two-pass: a match against a *demoted* inbox (a firehose like
    lkml, configured via `Settings.canonical_demoted_inboxes`) is
    only used as a fallback when no non-demoted match is found later
    in the walk. So `Cc: linux-kernel@vger.kernel.org,
    linux-arm-kernel@lists.infradead.org` pins to linux-arm-kernel
    even though lkml is positionally first. The premise of "first
    list-shaped address = author intent" holds well across topical
    lists but breaks against firehose-shaped lists that are
    routinely cc'd as a general-visibility broadcast; this layer
    encodes the convention that the topical list is the
    conversational home.
    """
    fallback: int | None = None
    for addr in addresses:
        inbox_id = address_to_inbox_id.get(addr)
        if inbox_id is None:
            continue
        if inbox_id in demoted_inbox_ids:
            if fallback is None:
                fallback = inbox_id
            continue
        return inbox_id
    return fallback


def fallback_canonical_name(
    canonical_id: int | None,
    links: list[tuple[int, str]],
    demoted_names: frozenset[str] | None = None,
) -> str | None:
    """Render-time canonical-inbox resolution. Uses `canonical_id`
    when set and present in `links`; otherwise alphabetically-first
    among the linked inboxes, with demoted names sorted to the back.

    Centralises the fallback rule used by `_canonical_inbox_name`,
    `_canonical_inbox_names_for`, and the per-subsystem `recent
    patches` surface. `demoted_names=None` defers to
    `Settings.canonical_demoted_inboxes`, so the typical call site
    (a render path with no plumbed setting) does the right thing
    without threading the value through.
    """
    if canonical_id is not None:
        for ix_id, name in links:
            if ix_id == canonical_id:
                return name
    if not links:
        return None
    if demoted_names is None:
        # Lazy import, kept for locality rather than for the reason
        # once given here. The old note claimed `canonical` is imported
        # before settings are "guaranteed-initialised"; both halves are
        # false. `mimir.config` imports no mimir module except
        # `mimir._outbound` (stdlib only), so a module-level import
        # cannot cycle, and `settings = Settings()` is built at config
        # import, so any successful import yields it initialised.
        # Verified by hoisting it to module scope and running the
        # canonical + message-route suites clean.
        from mimir.config import settings

        demoted_names = frozenset(settings.canonical_demoted_inboxes)
    names = [name for _, name in links]
    non_demoted = sorted(n for n in names if n not in demoted_names)
    if non_demoted:
        return non_demoted[0]
    return min(names)
