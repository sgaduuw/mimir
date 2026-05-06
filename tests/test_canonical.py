"""Pure-logic tests for `mimir.canonical`. Hits the heuristic, the
header-extraction, and the canonical-pick — no DB."""
from mimir.canonical import (
    LIST_HOST_SUFFIXES,
    extract_list_addresses,
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


def test_is_list_address_subdomain_match_accepted():
    # foo.vger.kernel.org should still count (defensive — covers
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
    # accidentally trimmed.
    assert "vger.kernel.org" in LIST_HOST_SUFFIXES
    assert "kvack.org" in LIST_HOST_SUFFIXES
    assert "lists.infradead.org" in LIST_HOST_SUFFIXES
    assert "lists.freedesktop.org" in LIST_HOST_SUFFIXES


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
