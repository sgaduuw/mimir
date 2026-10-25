"""Unit tests for `mimir.trailers.extract_trailers`, the
review-attestation extractor used at ingest and by the backfill CLI.
"""

import time

import pytest

from mimir.trailers import extract_trailers


def test_extract_returns_empty_for_none_or_empty():
    assert extract_trailers(None) == []
    assert extract_trailers("") == []


def test_extract_returns_empty_for_prose():
    body = "Hi all,\n\nI think we should review the wakeup logic.\n\nThanks,\nA\n"
    assert extract_trailers(body) == []


def test_extract_single_reviewed_by():
    body = "Reviewed-by: Alice Example <alice@example.com>\n"
    assert extract_trailers(body) == [
        ("Reviewed-by", "Alice Example", "alice@example.com"),
    ]


def test_extract_multiple_roles():
    """Each role lands its own row; the same person under two roles
    on one patch (Reported-by + Tested-by, common in stable backports)
    appears twice."""
    body = (
        "Signed-off-by: Author <author@example.com>\n"
        "Reviewed-by: Alice <alice@example.com>\n"
        "Acked-by: Bob <bob@kernel.org>\n"
        "Tested-by: Carol <carol@example.com>\n"
        "Reported-by: Carol <carol@example.com>\n"
    )
    rows = extract_trailers(body)
    assert ("Reviewed-by", "Alice", "alice@example.com") in rows
    assert ("Acked-by", "Bob", "bob@kernel.org") in rows
    assert ("Tested-by", "Carol", "carol@example.com") in rows
    assert ("Reported-by", "Carol", "carol@example.com") in rows
    # Signed-off-by is NOT indexed (author chain-of-custody, not a
    # review signal).
    assert all(role != "Signed-off-by" for role, _, _ in rows)
    assert len(rows) == 4


def test_extract_canonicalises_role_casing():
    """Bodies seen in the wild carry mixed casing: `reviewed-by:`,
    `Reviewed-By:`. The extractor returns the canonical capitalisation
    from `INDEXED_TRAILER_ROLES` regardless of what landed."""
    body = "reviewed-by: a@x.com\nREVIEWED-BY: b@x.com\nReviewed-By: c@x.com\n"
    rows = extract_trailers(body)
    assert all(role == "Reviewed-by" for role, _, _ in rows)
    assert {addr for _, _, addr in rows} == {"a@x.com", "b@x.com", "c@x.com"}


def test_extract_address_casing_preserved_verbatim():
    """The extractor stores the address verbatim; the caller
    (ingest) lowercases for `address_normalized`. This pins the
    contract: extract_trailers does NOT lowercase."""
    body = "Reviewed-by: A <Alice@Example.COM>\n"
    rows = extract_trailers(body)
    assert rows == [("Reviewed-by", "A", "Alice@Example.COM")]


def test_extract_bare_address_no_display_name():
    """Some trailers carry only an address, name is the empty string."""
    body = "Acked-by: <terse@example.com>\n"
    assert extract_trailers(body) == [
        ("Acked-by", "", "terse@example.com"),
    ]
    body_no_angles = "Acked-by: terse@example.com\n"
    assert extract_trailers(body_no_angles) == [
        ("Acked-by", "", "terse@example.com"),
    ]


def test_extract_ignores_quoted_trailers_in_replies():
    """Reviewers replying often quote the previous patch's trailer
    block. Line-start regex anchor means `> Reviewed-by: …` doesn't
    match, those attestations belong to the parent message and were
    already indexed there."""
    body = (
        "On Mon, A wrote:\n"
        "> Reviewed-by: Alice <alice@example.com>\n"
        "> Acked-by: Bob <bob@example.com>\n"
        "\n"
        "LGTM, will apply.\n"
    )
    assert extract_trailers(body) == []


def test_extract_skips_address_with_html_metacharacters():
    """`"`, `<` and `>` are outside RFC 5322's atext, so an address
    carrying one falls through (silently skipped). `&` and `'` are
    atext and are kept; rendering escapes the stored value."""
    body = 'Reviewed-by: A <"weird"@example.com>\n'
    assert extract_trailers(body) == []


def test_extract_handles_co_developed_by_and_compound_roles():
    """`Co-developed-by:` and `Reported-and-tested-by:` both contain
    hyphens; verify the literal-alternation regex matches them
    without needing word-boundary tricks."""
    body = "Co-developed-by: A <a@x.com>\nReported-and-tested-by: B <b@x.com>\n"
    rows = extract_trailers(body)
    assert ("Co-developed-by", "A", "a@x.com") in rows
    assert ("Reported-and-tested-by", "B", "b@x.com") in rows


def test_a_long_trailer_line_parses_in_linear_time():
    """The line pattern retried its trailing-whitespace tail at every
    character of a long whitespace run (#921)."""
    start = time.perf_counter()
    spaces = " " * 300_000
    assert extract_trailers("Reviewed-by: a" + spaces + "b") == []
    assert extract_trailers("Reviewed-by: a" + spaces + "b <j@x.org>") == [
        ("Reviewed-by", "a" + spaces + "b", "j@x.org")
    ]
    assert time.perf_counter() - start < 5


def test_a_long_address_run_parses_in_linear_time():
    """The name/address pattern rescanned the address run from every
    possible end of the name (#921)."""
    start = time.perf_counter()
    run = "a" * 300_000
    assert extract_trailers("Reviewed-by: " + run) == []
    assert extract_trailers("Reviewed-by: " + run + " <j@x.org>") == [
        ("Reviewed-by", run, "j@x.org")
    ]
    assert time.perf_counter() - start < 5


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Reviewed-by: X <x=y@example.com>", [("Reviewed-by", "X", "x=y@example.com")]),
        (
            "Acked-by: O'Neil <o'neil@example.com>",
            [("Acked-by", "O'Neil", "o'neil@example.com")],
        ),
        ("Reviewed-by: X x=y@example.com", [("Reviewed-by", "X", "x=y@example.com")]),
        # Not an address we can split safely: skipped, never split inside
        # the address (#913).
        ('Reviewed-by: A <x"y@example.com>', []),
        ("Reviewed-by: Name:a@example.com", []),
        ("Reviewed-by: name,a@example.com", []),
    ],
)
def test_extract_keeps_the_whole_rfc5322_local_part(line, expected):
    """A local-part character outside the old narrow class ended the
    address there, and the name took the rest: `X <x=` (#913)."""
    assert extract_trailers(line + "\n") == expected


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        ("Name<a@b.example>", ("Name", "a@b.example")),
        ("Name <a@b.example", ("Name", "a@b.example")),
        ("Name a@b.example>", ("Name", "a@b.example")),
        ('"Doe, J" <j@x.org>', ("Doe, J", "j@x.org")),
        ("Name (maint) <a@b.example>", ("Name (maint)", "a@b.example")),
        (
            "syzbot+abc@syzkaller.appspotmail.com",
            ("", "syzbot+abc@syzkaller.appspotmail.com"),
        ),
        ("a@b.example   ", ("", "a@b.example")),
        ("\nJoe <j@x.org>", ("Joe", "j@x.org")),
        ("Name <a@b.example> # v5.10", None),
    ],
)
def test_existing_trailer_shapes_parse_as_before(rest, expected):
    """Shapes that parsed right before #913 and must not move."""
    rows = extract_trailers("Tested-by: " + rest + "\n")
    assert rows == ([] if expected is None else [("Tested-by", *expected)])


def test_an_rfc5322_address_run_parses_in_linear_time():
    """`=` and `'` sit inside the address run since #913; the scan over
    that run must stay linear."""
    start = time.perf_counter()
    run = "a='" * 100_000
    assert extract_trailers("Reviewed-by: " + run) == []
    assert extract_trailers("Reviewed-by: " + run + " <j@x.org>") == [
        ("Reviewed-by", run, "j@x.org")
    ]
    assert time.perf_counter() - start < 5
