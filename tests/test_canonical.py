"""Pure-logic tests for `mimir.canonical`. Hits the heuristic, the
header-extraction, and the canonical-pick, no DB."""
from mimir.canonical import (
    LIST_HOST_SUFFIXES,
    extract_list_addresses,
    fallback_canonical_name,
    is_list_address,
    pick_canonical_inbox_id,
)


# is_list_address: conservative suffix filter


def test_is_list_address_known_kernel_org_list():
    assert is_list_address("linux-fsdevel@vger.kernel.org") is True


def test_is_list_address_known_kvack_list():
    assert is_list_address("linux-mm@kvack.org") is True


def test_is_list_address_known_freedesktop():
    assert is_list_address("dri-devel@lists.freedesktop.org") is True


def test_is_list_address_personal_address_rejected():
    assert is_list_address("alice@example.com") is False


def test_is_list_address_bare_kernel_org_rejected():
    # `kernel.org` is a personal-address domain (cve@, gregkh@,
    # torvalds@). List traffic lives on the subdomains (vger,
    # subspace, lists.linux.dev). Reported as a false-positive
    # in the off-list-parent hint UI, hence the explicit pin.
    assert is_list_address("cve@kernel.org") is False
    assert is_list_address("gregkh@kernel.org") is False


def test_is_list_address_subdomain_match_accepted():
    # foo.vger.kernel.org should still count (defensive, covers
    # whatever future relay shenanigans the kernel infra invents).
    assert is_list_address("listname@subdomain.vger.kernel.org") is True


def test_is_list_address_empty_inputs_rejected():
    assert is_list_address(None) is False
    assert is_list_address("") is False
    assert is_list_address("not-an-address") is False
    assert is_list_address("@") is False
    assert is_list_address("user@") is False


def test_is_list_address_case_insensitive():
    assert is_list_address("LKML@VGER.KERNEL.ORG") is True


def test_is_list_address_custom_suffix_set():
    suffixes = frozenset({"example.com"})
    assert is_list_address("alice@example.com", suffixes) is True
    assert is_list_address("alice@vger.kernel.org", suffixes) is False


def test_known_suffixes_cover_kernel_basics():
    # Smoke-check the baseline suffix set so we notice if it's
    # accidentally trimmed. `issubset` over set algebra rather than
    # per-element `<hostname-literal> in LIST_HOST_SUFFIXES`: the
    # latter shape is what `LIST_HOST_SUFFIXES` is actually for
    # (set membership, not URL substring matching), but CodeQL's
    # `py/incomplete-url-substring-sanitization` rule pattern-matches
    # the `<hostname-literal> in <thing>` shape regardless of the
    # RHS type, so the per-element form flagged 4 high-severity
    # alerts (#4-7) it had no actual basis for. The subset check
    # is also a tighter assertion: it pins the baseline set as a
    # contract instead of cherry-picking values.
    baseline = {
        "vger.kernel.org",
        "kvack.org",
        "lists.infradead.org",
        "lists.freedesktop.org",
    }
    assert baseline.issubset(LIST_HOST_SUFFIXES)


# extract_list_addresses: ordering, dedup, header parsing


def test_extract_to_then_cc_order():
    headers = {
        "To": "linux-fsdevel@vger.kernel.org",
        "Cc": "linux-kernel@vger.kernel.org",
    }
    assert extract_list_addresses(headers) == [
        "linux-fsdevel@vger.kernel.org",
        "linux-kernel@vger.kernel.org",
    ]


def test_extract_filters_personal_addresses():
    headers = {
        "To": "alice@example.com, linux-fsdevel@vger.kernel.org",
        "Cc": "bob@example.com",
    }
    assert extract_list_addresses(headers) == ["linux-fsdevel@vger.kernel.org"]


def test_extract_dedups_same_address_in_to_and_cc():
    headers = {
        "To": "linux-fsdevel@vger.kernel.org",
        "Cc": "linux-fsdevel@vger.kernel.org",
    }
    assert extract_list_addresses(headers) == ["linux-fsdevel@vger.kernel.org"]


def test_extract_lowercases():
    headers = {"To": "LKML@Vger.Kernel.Org"}
    assert extract_list_addresses(headers) == ["lkml@vger.kernel.org"]


def test_extract_handles_display_names():
    headers = {
        "To": '"Linux fs devs" <linux-fsdevel@vger.kernel.org>, "Other" <bob@example.com>',
    }
    assert extract_list_addresses(headers) == ["linux-fsdevel@vger.kernel.org"]


def test_extract_no_headers_returns_empty():
    assert extract_list_addresses({}) == []
    assert extract_list_addresses({"To": "", "Cc": ""}) == []


def test_extract_header_keys_case_insensitive():
    """RFC 5322 field names are case-insensitive; `mimir.parser`
    preserves whatever casing the wire delivered. Some ML re-mailers
    downcase headers, so a strict `headers.get("To")` silently
    returned `None` and broke canonical-inbox resolution plus the
    off-list-parent hint. Audit (2026-05-15) called it the silent
    canonical-break."""
    for to_key, cc_key in (
        ("to", "cc"),         # all lowercase (the regression case)
        ("TO", "CC"),         # all uppercase
        ("To", "cc"),         # mixed
        ("tO", "Cc"),         # weird mixed
    ):
        headers = {
            to_key: "linux-fsdevel@vger.kernel.org",
            cc_key: "linux-kernel@vger.kernel.org",
        }
        assert extract_list_addresses(headers) == [
            "linux-fsdevel@vger.kernel.org",
            "linux-kernel@vger.kernel.org",
        ], f"failed on keys ({to_key!r}, {cc_key!r})"


def test_extract_malformed_addresses_ignored():
    headers = {"To": "this is garbage, linux-fsdevel@vger.kernel.org"}
    # The non-address junk falls out of getaddresses cleanly; the real
    # list address survives.
    assert extract_list_addresses(headers) == ["linux-fsdevel@vger.kernel.org"]


# pick_canonical_inbox_id: first match wins


def test_pick_first_matching_address():
    addrs = ["linux-fsdevel@vger.kernel.org", "linux-kernel@vger.kernel.org"]
    mapping = {
        "linux-fsdevel@vger.kernel.org": 7,
        "linux-kernel@vger.kernel.org": 1,
    }
    # To-then-Cc order means linux-fsdevel wins, even though both match.
    assert pick_canonical_inbox_id(addrs, mapping) == 7


def test_pick_skips_unknown_addresses():
    addrs = ["unknown@vger.kernel.org", "linux-kernel@vger.kernel.org"]
    mapping = {"linux-kernel@vger.kernel.org": 1}
    assert pick_canonical_inbox_id(addrs, mapping) == 1


def test_pick_returns_none_when_no_match():
    addrs = ["unknown@vger.kernel.org"]
    mapping = {"linux-kernel@vger.kernel.org": 1}
    assert pick_canonical_inbox_id(addrs, mapping) is None


def test_pick_empty_inputs():
    assert pick_canonical_inbox_id([], {"x": 1}) is None
    assert pick_canonical_inbox_id(["x@vger.kernel.org"], {}) is None


# pick_canonical_inbox_id: demoted inboxes used only as fallback


def test_pick_skips_demoted_in_favour_of_later_non_demoted():
    """lkml positionally first in To/Cc should NOT win when a
    topical list appears later in the walk and isn't demoted.
    Reflects how kernel devs treat lkml as a firehose CC, not the
    conversational home, and matches Google's canonical pick."""
    addrs = [
        "linux-kernel@vger.kernel.org",          # demoted (lkml)
        "linux-arm-kernel@lists.infradead.org",  # topical, wins
    ]
    mapping = {
        "linux-kernel@vger.kernel.org": 1,
        "linux-arm-kernel@lists.infradead.org": 2,
    }
    assert pick_canonical_inbox_id(addrs, mapping, frozenset({1})) == 2


def test_pick_uses_demoted_when_only_demoted_matches():
    """A message that only landed on a demoted inbox (no topical
    list on To/Cc, no other inbox matches the walk) still
    canonicalises to the demoted inbox; the demotion is a
    preference between candidates, not an exclusion."""
    addrs = [
        "linux-kernel@vger.kernel.org",
        "alice@example.com",  # not list-shaped → no match
    ]
    mapping = {"linux-kernel@vger.kernel.org": 1}
    assert pick_canonical_inbox_id(addrs, mapping, frozenset({1})) == 1


def test_pick_non_demoted_match_before_demoted_in_walk_order():
    """When the non-demoted match appears positionally first AND the
    demoted match appears later, the first-non-demoted still wins
    (sanity companion: the demoted-tier logic doesn't accidentally
    re-order non-demoted picks)."""
    addrs = [
        "linux-arm-kernel@lists.infradead.org",
        "linux-kernel@vger.kernel.org",
    ]
    mapping = {
        "linux-arm-kernel@lists.infradead.org": 2,
        "linux-kernel@vger.kernel.org": 1,
    }
    assert pick_canonical_inbox_id(addrs, mapping, frozenset({1})) == 2


# fallback_canonical_name: render-time two-tier sort


def test_fallback_uses_explicit_canonical_id():
    assert fallback_canonical_name(
        canonical_id=7,
        links=[(1, "lkml"), (7, "linux-arm-kernel")],
        demoted_names=frozenset({"lkml"}),
    ) == "linux-arm-kernel"


def test_fallback_excludes_demoted_from_alphabetical():
    """`linux-arm-kernel` and `lkml` mixed: alphabetical would pick
    `linux-arm-kernel` anyway, but `mm-commits` vs `lkml` flips
    direction. Pin the demoted-to-back rule on the harder case."""
    assert fallback_canonical_name(
        canonical_id=None,
        links=[(1, "lkml"), (3, "mm-commits")],
        demoted_names=frozenset({"lkml"}),
    ) == "mm-commits"


def test_fallback_uses_demoted_when_no_non_demoted_link():
    assert fallback_canonical_name(
        canonical_id=None,
        links=[(1, "lkml")],
        demoted_names=frozenset({"lkml"}),
    ) == "lkml"


def test_fallback_empty_links_returns_none():
    assert fallback_canonical_name(
        canonical_id=None, links=[], demoted_names=frozenset({"lkml"}),
    ) is None


def test_fallback_defaults_to_settings_demoted():
    """When `demoted_names` is None, the helper reads from
    `Settings.canonical_demoted_inboxes` (default `['lkml']`). Pin
    that default so a render-path call with no plumbed setting
    still demotes lkml."""
    assert fallback_canonical_name(
        canonical_id=None,
        links=[(1, "lkml"), (3, "mm-commits")],
    ) == "mm-commits"
