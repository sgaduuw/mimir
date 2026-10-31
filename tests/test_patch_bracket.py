"""Which subjects count as a patch bracket (#711), checked through both
parsers. `parse_cover_letter` and `parse_in_series_patch_subject` share
one predicate (`patch_bracket`), so one table drives them both: a cover
letter is the `0/N` shape, an in-series patch the `N/M` shape (N >= 1),
and a bracket either counts for both or for neither."""

import re

import pytest

from mimir.patch_revisions import (
    ParsedPatchSubject,
    parse_in_series_patch_subject,
    patch_bracket,
    subject_tags,
)
from mimir.patch_series import CoverLetter, parse_cover_letter, series_key

# (bracket template, counts). `{n}` becomes `0` for a cover letter and
# `1` for an in-series patch. Shapes without PATCH are the narrowed
# option 2 of #711: N/M, another word, no PULL or ANNOUNCE, no colon.
BRACKETS = [
    ("PATCH {n}/3", True),
    ("PATCH v2 {n}/3", True),
    ("net-next v7 {n}/10", True),
    ("RFC {n}/6", True),
    ("RFC v2 {n}/9", True),
    ("v2 {n}/1", True),
    ("CI {n}/6", True),
    ("BlueZ v10 {n}/12", True),
    ("34-longterm {n}/179", True),
    ("linux-next:master {n}/2056", False),
    ("openeuler:OLK-6.6 {n}/13", False),
    ("PULL {n}/16", False),
    ("GIT PULL {n}/3", False),
    ("CONSOLIDATED PULL {n}/4", False),
    ("kvm-unit-tests GIT PULL {n}/4", False),
    ("pull {n}/4", False),
    ("ANNOUNCE {n}/3", False),
    ("{n}/1", False),
    (" {n}/1", False),
]
TITLE = "foo: do bar"


def _cover(inside):
    return parse_cover_letter(f"[{inside.format(n=0)}] {TITLE}")


def _in_series(inside):
    return parse_in_series_patch_subject(f"[{inside.format(n=1)}] {TITLE}")


@pytest.mark.parametrize(("inside", "counts"), BRACKETS)
def test_cover_letter_bracket_table(inside, counts):
    assert (_cover(inside) is not None) is counts


@pytest.mark.parametrize(("inside", "counts"), BRACKETS)
def test_in_series_bracket_table(inside, counts):
    assert (_in_series(inside) is not None) is counts


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("[RFC 0/6] t", CoverLetter(version="rfc", total=6, title="t")),
        ("[v2 0/1] t", CoverLetter(version="v2", total=1, title="t")),
        ("[CI 0/6] t", CoverLetter(version="v1", total=6, title="t")),
        ("[BlueZ v10 00/12] t", CoverLetter(version="v10", total=12, title="t")),
        ("[34-longterm 000/179] t", CoverLetter(version="v1", total=179, title="t")),
        ("[RFC v2 0/9] t", CoverLetter(version="v2", total=9, title="t")),
        ("[patch 0/3] t", CoverLetter(version="v1", total=3, title="t")),
        ("[Patch v2 0/3] t", CoverLetter(version="v2", total=3, title="t")),
        ("[U-Boot] [PATCH 0/3] t", CoverLetter(version="v1", total=3, title="t")),
        (
            "[Qemu-devel] [PATCH v2 0/2] t",
            CoverLetter(version="v2", total=2, title="t"),
        ),
        (
            "[Buildroot] [PATCH -next 0/2] t",
            CoverLetter(version="v1", total=2, title="t"),
        ),
        (
            "[U-Boot-Users] [patch RFC 0/11] t",
            CoverLetter(version="rfc", total=11, title="t"),
        ),
    ],
)
def test_cover_letter_shapes(subject, expected):
    assert parse_cover_letter(subject) == expected


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("[net-next v7 02/10] t", ParsedPatchSubject(2, 10, "t")),
        ("[RFC v2 1/9] t", ParsedPatchSubject(1, 9, "t")),
        ("[patch 1/3] t", ParsedPatchSubject(1, 3, "t")),
        ("[U-Boot] [PATCH 2/3] t", ParsedPatchSubject(2, 3, "t")),
        ("[Qemu-devel] [PATCH v2 1/2] t", ParsedPatchSubject(1, 2, "t")),
        ("[U-Boot-Users] [patch #1/11] t", ParsedPatchSubject(1, 11, "t")),
    ],
)
def test_in_series_shapes(subject, expected):
    assert parse_in_series_patch_subject(subject) == expected


@pytest.mark.parametrize(
    "subject",
    [
        "[A] [B] [PATCH 0/3] t",  # one leading tag only
        "[U-Boot] [0/3] t",  # the second bracket must carry PATCH
        "[U-Boot] [PULL 0/3] t",
        "Re: [U-Boot] [PATCH 0/3] t",  # a reply is not a cover letter
        "[U-Boot] [PATCH 0/3]",  # no title
    ],
)
def test_cover_letter_rejects(subject):
    assert parse_cover_letter(subject) is None


def test_date_like_bracket_without_patch_is_rejected_by_both_parsers():
    """`2024/11` is a date, not patch 2024 of 11: without a PATCH token
    the position must not exceed the total."""
    assert parse_cover_letter("[report 2024/11] x") is None
    assert parse_in_series_patch_subject("[report 2024/11] x") is None


def test_position_beyond_total_still_parses_with_patch_token():
    """A typo like `4/3` keeps parsing when the bracket says PATCH."""
    assert parse_in_series_patch_subject("[PATCH v2 4/3] x") == ParsedPatchSubject(
        4, 3, "x"
    )
    assert parse_in_series_patch_subject("[RFC 2/6] x") == ParsedPatchSubject(2, 6, "x")


def test_list_tag_is_not_part_of_the_series_key():
    tagged = parse_cover_letter("[U-Boot] [PATCH 0/3] t")
    plain = parse_cover_letter("[PATCH 0/3] t")
    assert tagged is not None
    assert plain is not None
    author = "A <a@example.org>"
    assert series_key(tagged.title, author) == series_key(plain.title, author)


AGREEMENT_SUBJECTS = [
    *(f"[{b.format(n=n)}] t" for b, _ in BRACKETS for n in (0, 1)),
    "[U-Boot] [PATCH 0/3] t",
    "[U-Boot] [PATCH v2 1/2] t",
    "[U-Boot-Users] [patch #1/11] t",
    "[A] [B] [PATCH 0/3] t",
    "[U-Boot] [0/3] t",
    "[report 2024/11] t",
    "[PATCH v2 4/3] t",
    "Re: [PATCH 1/3] t",
]


def test_both_parsers_agree_on_patch_brackets():
    seen = 0
    for subject in AGREEMENT_SUBJECTS:
        assert re.search(r"\d/\d", subject)  # every subject has an N/M
        decided = patch_bracket(subject) is not None
        parsed = (
            parse_cover_letter(subject) is not None
            or parse_in_series_patch_subject(subject) is not None
        )
        assert parsed is decided, subject
        seen += decided
    assert seen > 20  # the table holds plenty of accepted subjects


def test_patch_bracket_returns_the_deciding_bracket_and_title():
    assert patch_bracket("[U-Boot] [PATCH v2 1/2] t") == ("PATCH v2 1/2", "t")
    assert patch_bracket("[PATCH v2 1/2] t") == ("PATCH v2 1/2", "t")
    assert patch_bracket("[RFC 0/6] t") == ("RFC 0/6", "t")
    assert patch_bracket("[PULL 0/6] t") is None


@pytest.mark.parametrize(
    ("subject", "tags", "title"),
    [
        (
            "[LSF/MM/BPF TOPIC][RFC PATCH v4 00/27] Private Memory Nodes (w/ Compressed RAM)",
            ["LSF/MM/BPF TOPIC", "RFC", "v4"],
            "Private Memory Nodes (w/ Compressed RAM)",
        ),
        ("[PATCH v2 3/9] foo: bar", ["v2"], "foo: bar"),
        ("[PATCH] foo: bar", [], "foo: bar"),
        ("[PATCHv3 1/2] foo", ["v3"], "foo"),
        ("[net-next v7 02/10] foo", ["net-next", "v7"], "foo"),
        ("[U-Boot] [PATCH 0/3] title", ["U-Boot"], "title"),
        # Not a patch: nothing is stripped, a prefix may be the whole point.
        ("[GIT PULL] fixes for -rc2", [], "[GIT PULL] fixes for -rc2"),
        ("Re: [PATCH v2 1/3] foo", [], "Re: [PATCH v2 1/3] foo"),
        ("plain subject", [], "plain subject"),
        ("", [], ""),
    ],
)
def test_subject_tags_split_the_prefix_from_the_title(subject, tags, title):
    """The thread header's tags and its stripped title come from the one
    bracket `patch_bracket` decides on (#989); a subject that is not a
    patch keeps every character."""
    assert subject_tags(subject) == (tags, title)


def test_subject_tags_agree_with_patch_bracket_on_every_title():
    """Whatever `patch_bracket` calls the title, `subject_tags` returns."""
    for template, counts in BRACKETS:
        subject = f"[{template.format(n=1)}] foo: bar"
        got = patch_bracket(subject)
        assert (got is not None) is counts, subject
        if got is not None:
            assert subject_tags(subject)[1] == got[1], subject


def test_subject_tags_parse_hostile_subjects_in_linear_time():
    """A sender writes the subject (AGENTS.md test rule 7)."""
    import time

    start = time.perf_counter()
    for subject in (
        "[" * 300_000,
        "[a" * 300_000,
        "[PATCH " + "v1 " * 300_000 + "]",
        "[x][" * 100_000,
        "[PATCH 1/2" + " 1/2" * 300_000,
    ):
        subject_tags(subject)
    assert time.perf_counter() - start < 5


def test_subject_tags_cap_what_a_sender_can_make_the_header_show():
    tags, _ = subject_tags("[PATCH " + "x" * 500 + " a b c d e f g h 1/2] t")
    assert len(tags) == 6
    assert all(len(t) <= 40 for t in tags)
