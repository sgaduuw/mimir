"""Unit tests for `mimir.trailers.extract_trailers` — the
review-attestation extractor used at ingest and by the backfill CLI.
"""
from mimir.trailers import extract_trailers


def test_extract_returns_empty_for_none_or_empty():
    assert extract_trailers(None) == []
    assert extract_trailers("") == []


def test_extract_returns_empty_for_prose():
    body = (
        "Hi all,\n\n"
        "I think we should review the wakeup logic.\n\n"
        "Thanks,\nA\n"
    )
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
    body = (
        "reviewed-by: a@x.com\n"
        "REVIEWED-BY: b@x.com\n"
        "Reviewed-By: c@x.com\n"
    )
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
    """Some trailers carry only an address — name is the empty string."""
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
    match — those attestations belong to the parent message and were
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
    """The address regex is conservative — addresses carrying HTML
    metacharacters fall through (silently skipped). Defense in depth
    for any future render path that touches the indexed value."""
    body = 'Reviewed-by: A <"weird"@example.com>\n'
    assert extract_trailers(body) == []


def test_extract_handles_co_developed_by_and_compound_roles():
    """`Co-developed-by:` and `Reported-and-tested-by:` both contain
    hyphens; verify the literal-alternation regex matches them
    without needing word-boundary tricks."""
    body = (
        "Co-developed-by: A <a@x.com>\n"
        "Reported-and-tested-by: B <b@x.com>\n"
    )
    rows = extract_trailers(body)
    assert ("Co-developed-by", "A", "a@x.com") in rows
    assert ("Reported-and-tested-by", "B", "b@x.com") in rows
