"""Unit tests for `mimir.patch_series`. Detection + key stability."""
from mimir.patch_series import (
    CoverLetter,
    parse_cover_letter,
    series_key,
)


# parse_cover_letter, recognising shapes.


def test_recognises_basic_cover_letter():
    out = parse_cover_letter("[PATCH 0/3] improve foo handling")
    assert out == CoverLetter(version="v1", total=3, title="improve foo handling")


def test_recognises_versioned_cover_letter():
    out = parse_cover_letter("[PATCH v2 0/3] improve foo handling")
    assert out == CoverLetter(version="v2", total=3, title="improve foo handling")


def test_recognises_rfc_cover_letter():
    out = parse_cover_letter("[PATCH RFC 0/3] explore foo redesign")
    assert out == CoverLetter(version="rfc", total=3, title="explore foo redesign")


def test_explicit_version_wins_over_resend():
    """`[PATCH RESEND v3 0/3]` is a resent v3, not an RFC-shaped
    series. The explicit `v\\d+` marker takes precedence."""
    out = parse_cover_letter("[PATCH RESEND v3 0/3] improve foo")
    assert out is not None
    assert out.version == "v3"


def test_explicit_version_wins_over_rfc_regardless_of_order():
    """The audit (2026-05-15) flagged the for/else version-selection
    as accidentally-correct: it depended on `re.findall`'s left-to-
    right ordering plus the loop's fall-through. Pin the precedence
    in both orders so a future regex rewrite that flips findall's
    order would surface."""
    # RFC before v3
    rfc_first = parse_cover_letter("[PATCH RFC v3 0/3] explore foo")
    assert rfc_first is not None and rfc_first.version == "v3"
    # v3 before RFC
    v_first = parse_cover_letter("[PATCH v3 RFC 0/3] explore foo")
    assert v_first is not None and v_first.version == "v3"


def test_marker_fallback_when_no_explicit_v_version():
    """No `v\\d+` token but `RFC` is present → version falls back to
    the marker. Documents the "no v\\d+ → first marker" branch."""
    out = parse_cover_letter("[PATCH RFC 0/3] explore foo")
    assert out is not None
    assert out.version == "rfc"


def test_recognises_subsystem_tag_in_bracket():
    """`[PATCH net 0/3]` and `[PATCH net-next v2 0/3]` are common
    in net-related submissions. The subsystem tag is tolerated;
    the title and version still parse cleanly."""
    out = parse_cover_letter("[PATCH net-next v2 0/12] netdev refactor")
    assert out == CoverLetter(version="v2", total=12, title="netdev refactor")


# parse_cover_letter, rejection shapes.


def test_rejects_reply_to_cover_letter():
    """Reply subjects start with `Re:`, not `[`. The bracket-at-
    column-0 anchor keeps replies out of the cover-letter set."""
    assert parse_cover_letter("Re: [PATCH v2 0/3] improve foo") is None


def test_rejects_individual_patch_subject():
    """`[PATCH 1/3]` etc. are individual patches, not cover
    letters. Only `0/N` qualifies."""
    assert parse_cover_letter("[PATCH 1/3] foo: add bar") is None
    assert parse_cover_letter("[PATCH v2 2/3] foo: refactor baz") is None


def test_rejects_non_patch_zero_of_n():
    """`[ANNOUNCE 0/3]` and `[GIT PULL 0/3]` aren't patch series
    cover letters, even though they share the 0/N shape. The
    PATCH token discriminator catches these."""
    assert parse_cover_letter("[ANNOUNCE 0/3] release plan") is None
    assert parse_cover_letter("[GIT PULL 0/3] tree updates") is None


def test_rejects_single_patch_subject():
    """No `0/N` shape in `[PATCH] foo: add bar`, single-patch
    submissions don't form a series in this slice's sense."""
    assert parse_cover_letter("[PATCH] foo: add bar") is None
    assert parse_cover_letter("[PATCH v2] foo: add bar") is None


def test_rejects_empty_and_none_inputs():
    assert parse_cover_letter(None) is None
    assert parse_cover_letter("") is None
    assert parse_cover_letter("   ") is None


def test_rejects_high_low_pattern_resembling_zero_of_n():
    """`10/30` would naively look like `0/N` if we didn't anchor
    on the leading digit. Pin the `\\b0/` shape."""
    assert parse_cover_letter("[PATCH 10/30] foo: x") is None


# series_key, stability across cosmetic drift.


def test_series_key_stable_across_version_reposts():
    """Same title + same author → same key regardless of how the
    version-marker brackets read on each repost."""
    a = series_key("improve foo handling", "Alice <a@example>")
    b = series_key("improve foo handling", "Alice <a@example>")
    assert a == b


def test_series_key_normalises_title_whitespace_and_case():
    """Cosmetic drift in the title (case, whitespace) shouldn't
    fork a series. v2 reposters routinely tidy spacing without
    intending a fresh thread."""
    a = series_key("improve foo handling", "Alice <a@example>")
    b = series_key("Improve  foo   Handling", "Alice <a@example>")
    assert a == b


def test_series_key_uses_email_address_not_display_name():
    """Display-name change (Alice → Alice (Cambridge)) is common
    when contributors update their MAINTAINERS-style qualifier.
    The series identity is on the email address."""
    a = series_key("improve foo", "Alice <a@example>")
    b = series_key("improve foo", "Alice (Cambridge) <a@example>")
    assert a == b


def test_series_key_address_case_normalised():
    """Some mail clients capitalise local-parts; email addresses
    are RFC-5321 case-insensitive in the domain part and de-facto
    case-insensitive in the local-part across the kernel
    community. Treat them so."""
    a = series_key("foo", "Alice <a@Example.com>")
    b = series_key("foo", "Alice <A@example.com>")
    assert a == b


def test_series_key_differs_for_different_authors():
    """Two authors posting series with the same title should NOT
    collapse to one timeline."""
    a = series_key("improve foo", "Alice <a@example>")
    b = series_key("improve foo", "Bob <b@example>")
    assert a != b


def test_series_key_differs_for_different_titles():
    a = series_key("improve foo", "Alice <a@example>")
    b = series_key("improve bar", "Alice <a@example>")
    assert a != b


def test_series_key_is_opaque_hash_not_plaintext():
    """The key is SHA-1 hex, deliberately not the raw
    `<author>|<title>` string. Storing the raw form would leak
    the author's email through any query or diagnostic dump,
    defeating the visible-HTML redaction posture."""
    key = series_key("foo", "Alice <a@example>")
    assert "@" not in key
    assert "alice" not in key.lower()
    assert "foo" not in key
    # SHA-1 hex = 40 chars.
    assert len(key) == 40
    assert all(c in "0123456789abcdef" for c in key)


def test_series_key_handles_missing_author():
    """None and empty author both produce the same key, both map
    to an empty address. A garbage-shaped author (no angle
    brackets, no @) gets a different key: `parseaddr` reads the
    whole string as the address part, which is fine, garbage in,
    garbage out, but stable."""
    assert series_key("foo", None) == series_key("foo", "")
    # Garbage input is its own bucket, won't collide with the
    # None/empty bucket.
    assert series_key("foo", "garbage") != series_key("foo", None)
