"""Unit tests for `mimir.patch_series`. Detection + key stability."""

import time

from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.patch_series import (
    CoverLetter,
    _bracket_version,
    _explicit_version,
    parse_cover_letter,
    posting_flags,
    reply_to_cover,
    reply_to_patch,
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


def test_recognises_version_glued_to_patch():
    """`[PATCHv2 0/9]` has no word boundary between PATCH and v2; it is
    still v2 (#816)."""
    out = parse_cover_letter("[PATCHv2 0/9] improve foo handling")
    assert out == CoverLetter(version="v2", total=9, title="improve foo handling")


def test_glued_version_keeps_other_markers_and_subsystem():
    for subject, version in [
        ("[PATCHv3 RESEND 0/3] foo", "v3"),
        ("[PATCHv2 net-next 0/3] foo", "v2"),
        ("[patchv12 0/3] foo", "v12"),
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == version, subject


def test_glued_version_with_leading_bracket_or_tag():
    """`PATCHv2` is a PATCH token too, so the bracket is still found
    behind another bracket or among other words (#816)."""
    for subject in [
        "[U-Boot] [PATCHv2 0/3] foo",
        "[RFC] [PATCHv2 0/3] foo",
        "[PATCHv2 net:x 0/3] foo",
        "[RFC PATCHv2 0/3] foo",
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == "v2", subject


def test_version_glued_to_rfc_resend_or_patches():
    """A `vN` glued to RFC, RESEND, PATCHES or PATCHSET is the version,
    as after PATCH (#819), and RFC and RESEND still count as markers."""
    for subject, version in [
        ("[PATCH RFCv9 0/4] foo", "v9"),
        ("[RFCv4 PATCH 0/6] foo", "v4"),
        ("[RFCv2 0/5] foo", "v2"),
        ("[RESENDv2 PATCH 0/3] foo", "v2"),
        ("[PATCHESv2 0/3] foo", "v2"),
        ("[PATCHSETv3 0/3] foo", "v3"),
        ("[PATCH rfcv3 0/2] foo", "v3"),
        ("[RESEND RFCv2 0/1] foo", "v2"),
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == version, (subject, out)
    assert posting_flags("[PATCH RFCv9 0/4] foo", "v9") == (True, False)
    assert posting_flags("[RESENDv2 PATCH 0/3] foo", "v2") == (False, True)
    assert posting_flags("[RESEND RFCv2 0/1] foo", "v2") == (True, True)
    # A glued marker only counts at the start of its word.
    assert parse_cover_letter("[PATCH xRFCv2 0/3] foo").version == "v1"


def test_a_dotted_version_in_a_branch_name_is_not_the_revision():
    """`v2019.04-aspeed-openbmc` or `v6.12-rc1` names a base tree, not a
    revision; the real `vN`, or the RFC beside it, wins (#819)."""
    for subject, version in [
        ("[PATCH u-boot v2019.04-aspeed-openbmc v2 0/4] foo", "v2"),
        ("[PATCH mpam/v6.10-rc1 RFC 0/4] foo", "rfc"),
        ("[AUFS PATCH v2.6.26-rc2-mm1 00/39] foo", "v1"),
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == version, (subject, out)


def test_a_parenthesised_or_dotted_base_tree_tag_is_not_the_revision():
    """`(v20.08)` and `v5.10` ahead of the real version tag a base tree;
    the later plain `vN` wins (#870). A lone dotted `v4.1` still reads
    as v4 (the shapes test below)."""
    for subject, version in [
        ("[PATCH (v20.08) v3 0/8] foo", "v3"),
        ("[PATCH v5.10 v2 0/2] foo", "v2"),
        ("[PATCH (v20.08) 0/8] foo", "v1"),
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == version, (subject, out)
    patch = parse_in_series_patch_subject("[PATCH v5.10 v2 1/2] foo: bar")
    assert patch is not None and patch.position == 1
    assert _bracket_version("PATCH v5.10 v2 1/2") == "v2"


def test_the_cross_revision_guard_reads_the_same_version():
    """`_explicit_version` is what the #862 guard compares: a cover and
    its patches agree on v3 despite the base-tree tag (#870)."""
    for subject, version in [
        ("[PATCH (v20.08) v3 0/8] foo", "v3"),
        ("[PATCH v3 1/8] foo: bar", "v3"),
        ("[PATCH v5.10 v2 1/2] foo: bar", "v2"),
        ("[PATCH 1/2] foo: bar", None),
    ]:
        assert _explicit_version(subject) == version, subject


def test_a_parenthesised_marker_or_plain_version_still_counts():
    for bracket, version in [
        ("PATCH (RFC) 0/3", "rfc"),
        ("PATCH (RESEND) 0/3", "resend"),
        ("PATCH (v2) 0/3", "v2"),
        ("PATCH 0/3 (v2)", "v2"),
    ]:
        assert _bracket_version(bracket) == version, bracket
    assert _explicit_version("[PATCH (v2) 1/3] foo: bar") == "v2"
    assert posting_flags("[PATCH v2 (RFC) 0/3] foo", "v2") == (True, False)
    assert posting_flags("[PATCH (RFC v2) 0/3] foo", "v2") == (True, False)


def test_a_base_tree_branch_name_is_not_the_revision():
    """`for-v6.18` and `stable/v3.2.x` name a tree (#870); `PATCH/v2`
    and `RFC/v2` stay versions."""
    for bracket, version in [
        ("PATCH for-v6.18 0/3", "v1"),
        ("PATCH stable/v3.2.x 0/3", "v1"),
        ("PATCH stable/v3.2.x v2 0/3", "v2"),
        ("PATCH for-v6.18 RFC 0/3", "rfc"),
        ("PATCH net-v2 0/2", "v2"),
        ("PATCH/v2 0/3", "v2"),
        ("RFC/v2 0/3", "v2"),
    ]:
        assert _bracket_version(bracket) == version, bracket
    assert _explicit_version("[PATCH stable/v3.2.x 1/2] foo: bar") is None


def test_a_long_bracket_parses_in_linear_time():
    """Reading the text after each match copied it every time (#870)."""
    start = time.perf_counter()
    assert _bracket_version("PATCH " + "v1 " * 300_000) == "v1"
    assert _bracket_version("PATCH " + "a/v1. " * 300_000) == "v1"
    assert time.perf_counter() - start < 1


def test_unchanged_version_shapes_stay_put():
    """Regressions for #870: none of these may move."""
    for bracket, version in [
        ("PATCH v3 1/2", "v3"),
        ("PATCHv3 1/2", "v3"),
        ("RFC PATCH v2 1/2", "v2"),
        ("PATCH v2.1 1/3", "v2"),
        ("PATCH 6.12-rc1 v2 1/2", "v2"),
        ("PATCH net-next v4 0/5", "v4"),
    ]:
        assert _bracket_version(bracket) == version, bracket


def test_shapes_the_version_rule_must_keep():
    """Measured on production before #819: these parsed right and must
    keep doing so."""
    for subject, version in [
        ("[PATCH/RFC 0/4] foo", "rfc"),
        ("[PATCHv9-rebased 00/37] foo", "v9"),
        ("[PATCHv2-next 0/3] foo", "v2"),
        ("[PATCH v4.1 0/3] foo", "v4"),
        ("[PATCH v6-resend 00/17] foo", "v6"),
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == version, (subject, out)


def test_glued_version_in_series_patch_is_detected():
    out = parse_in_series_patch_subject("[U-Boot] [PATCHv2 1/3] foo: add bar")
    assert out is not None and out.position == 1


def test_v_inside_another_word_is_not_a_version():
    """Only PATCH, PATCHES, PATCHSET, RFC or RESEND may sit directly
    before `v\\d+`; a `v2` buried in another token stays v1, and RFC
    glued to PATCH is not a marker."""
    for subject in [
        "[PATCH nv2 0/3] foo",
        "[PATCH drv2 0/3] foo",
        "[PATCH foov2bar 0/3] foo",
        "[PATCHRFC 0/3] foo",
        "[PATCHvx 0/3] foo",
    ]:
        out = parse_cover_letter(subject)
        assert out is not None and out.version == "v1", subject


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


def test_recognises_zero_padded_cover_letter():
    """`git format-patch --numbered` column-aligns positions when
    the series has >=10 patches: `00/27`, `01/27`, ..., `27/27`.
    The cover letter shape is `00/N`, not `0/N`. Regression for #247."""
    out = parse_cover_letter("[PATCH v3 00/27] Rust HRT support")
    assert out is not None
    assert out.version == "v3"
    assert out.total == 27
    # And the unversioned shape.
    out = parse_cover_letter("[PATCH 00/12] foo: cleanups")
    assert out is not None
    assert out.version == "v1"
    assert out.total == 12
    # Three-digit padding shape (rare but valid: >=100-patch series).
    out = parse_cover_letter("[PATCH v2 000/100] huge refactor")
    assert out is not None
    assert out.version == "v2"
    assert out.total == 100


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


# reply_to_cover, a cover-shaped subject that answers a cover (#820).

COVER = "[PATCH v2 0/3] improve foo\thandling"


def test_reply_to_cover_table():
    """Each row: (subject, its author, parent subject, parent author,
    expected). A reply repeats the parent cover's posting (title and
    version) from another address; a re-post from the same address
    stays a cover."""
    rows = [
        # Reviewer retyped the folded tab as a space: a reply.
        ("[PATCH v2 0/3] improve foo handling", "Bob <b@x>", COVER, "Al <a@x>", True),
        # Same address, other display name: the sender's own re-post.
        ("[PATCH v2 0/3] improve foo handling", "Al W <A@x>", COVER, "Al <a@x>", False),
        # The next revision In-Reply-To the last one.
        ("[PATCH v3 0/3] improve foo handling", "Bob <b@x>", COVER, "Al <a@x>", False),
        # Its own parent (a self-referencing In-Reply-To).
        (COVER, "Al <a@x>", COVER, "Al <a@x>", False),
        # Parent is a patch, a reply, or missing.
        (COVER, "Bob <b@x>", "[PATCH v2 1/3] foo: add bar", "Al <a@x>", False),
        (COVER, "Bob <b@x>", "Re: " + COVER, "Al <a@x>", False),
        (COVER, "Bob <b@x>", None, None, False),
        # Another title.
        ("[PATCH v2 0/3] improve bar", "Bob <b@x>", COVER, "Al <a@x>", False),
        # RFC answering RFC.
        (
            "[RFC 0/2] explore foo",
            "Bob <b@x>",
            "[RFC 0/2] explore foo",
            "Al <a@x>",
            True,
        ),
        # Another patch count: a re-post, measured on production.
        ("[PATCH v2 0/4] improve foo handling", "Bob <b@x>", COVER, "Al <a@x>", False),
        # A RESEND in one subject only: a re-post; in both: a reply.
        (
            "[RESEND PATCH v2 0/3] improve foo handling",
            "Bob <b@x>",
            COVER,
            "Al <a@x>",
            False,
        ),
        (COVER, "Bob <b@x>", "[RESEND PATCH v2 0/3] improve foo", "Al <a@x>", False),
        (
            "[RESEND PATCH v2 0/3] improve foo",
            "Bob <b@x>",
            "[PATCH RESEND v2 0/3] improve foo",
            "Al <a@x>",
            True,
        ),
    ]
    for subject, author, parent_subject, parent_author, expected in rows:
        assert parse_cover_letter(subject) is not None, subject
        got = reply_to_cover(subject, author, parent_subject, parent_author)
        assert got is expected, (subject, author, parent_subject)


# reply_to_patch, an in-series subject that answers that patch (#837).

PATCH = "[PATCH v2 1/3] foo: add\tbar"


def test_reply_to_patch_table():
    """Each row: (subject, its author, parent subject, parent author,
    touches files, expected). A reply repeats the parent patch's posting
    (position, total, title, version, RESEND) from any address and
    carries no diff; a diff makes it a re-post."""
    reply = "[PATCH v2 1/3] foo: add bar"
    rows = [
        (reply, "Bob <b@x>", PATCH, "Al <a@x>", False, True),
        # A diff: a re-post of the patch, measured on production.
        (reply, "Bob <b@x>", PATCH, "Al <a@x>", True, False),
        # Same address, other display name: the author answering their own
        # patch is a reply too (#860); with a diff it is a re-post.
        (reply, "Al W <A@x>", PATCH, "Al <a@x>", False, True),
        (reply, "Al W <A@x>", PATCH, "Al <a@x>", True, False),
        # Another position, total, version or title.
        ("[PATCH v2 2/3] foo: add bar", "Bob <b@x>", PATCH, "Al <a@x>", False, False),
        ("[PATCH v2 1/4] foo: add bar", "Bob <b@x>", PATCH, "Al <a@x>", False, False),
        ("[PATCH v3 1/3] foo: add bar", "Bob <b@x>", PATCH, "Al <a@x>", False, False),
        ("[PATCH v2 1/3] foo: add baz", "Bob <b@x>", PATCH, "Al <a@x>", False, False),
        # A RESEND in one subject only: a re-post; in both: a reply.
        (
            "[RESEND PATCH v2 1/3] foo: add bar",
            "Bob <b@x>",
            PATCH,
            "Al <a@x>",
            False,
            False,
        ),
        (
            "[RESEND PATCH v2 1/3] foo: add bar",
            "Bob <b@x>",
            "[PATCH RESEND v2 1/3] foo: add bar",
            "Al <a@x>",
            False,
            True,
        ),
        # Parent is a cover, a "Re:" reply, or missing.
        (reply, "Bob <b@x>", "[PATCH v2 0/3] foo: add bar", "Al <a@x>", False, False),
        (reply, "Bob <b@x>", "Re: " + PATCH, "Al <a@x>", False, False),
        (reply, "Bob <b@x>", None, None, False, False),
    ]
    for subject, author, parent_subject, parent_author, files, expected in rows:
        assert parse_in_series_patch_subject(subject) is not None, subject
        got = reply_to_patch(subject, author, parent_subject, parent_author, files)
        assert got is expected, (subject, author, parent_subject, files)


def test_reply_to_patch_leaves_covers_to_reply_to_cover():
    """A cover answering a cover is #820's rule, not this one."""
    cover = "[PATCH v2 0/3] foo: add bar"
    assert reply_to_cover(cover, "Bob <b@x>", cover, "Al <a@x>")
    assert not reply_to_patch(cover, "Bob <b@x>", cover, "Al <a@x>", False)
