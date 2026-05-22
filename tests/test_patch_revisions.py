"""Unit tests for `mimir.patch_revisions`: the in-series patch
subject parser, the cover-letter thread resolver, and the
cross-revision position matcher.

The resolver tests build real Article rows in the seeded DB so the
thread-walk SQL path is exercised end-to-end. The matcher tests
construct `InSeriesPatch` dataclasses directly (no DB) so the
matching logic is isolated from query shape.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import Article, ArticleFile, ArticleList, Inbox
from mimir.patch_revisions import (
    AMBIGUOUS_MATCH,
    InSeriesPatch,
    compute_revision_diff,
    match_revision_position,
    parse_in_series_patch_subject,
    resolve_series_patches,
)


# --- parse_in_series_patch_subject ---


def test_parse_in_series_patch_subject_basic():
    p = parse_in_series_patch_subject("[PATCH 1/3] foo: do bar")
    assert p is not None
    assert (p.position, p.total, p.canonical_subject) == (1, 3, "foo: do bar")


def test_parse_in_series_patch_subject_with_version_marker():
    p = parse_in_series_patch_subject("[PATCH v2 1/3] foo: do bar")
    assert p is not None
    assert (p.position, p.total) == (1, 3)


def test_parse_in_series_patch_subject_with_subsystem_tag():
    p = parse_in_series_patch_subject("[PATCH net-next v2 1/3] foo: do bar")
    assert p is not None
    assert (p.position, p.total, p.canonical_subject) == (1, 3, "foo: do bar")


def test_parse_in_series_patch_subject_rejects_cover_letter():
    """`[PATCH ... 0/N]` cover letters have a dedicated parser
    upstream (`patch_series.parse_cover_letter`); we filter them
    out here so resolve_series_patches doesn't double-count."""
    assert parse_in_series_patch_subject("[PATCH 0/3] series title") is None
    assert parse_in_series_patch_subject("[PATCH v2 0/3] series title") is None


def test_parse_in_series_patch_subject_rejects_solo_patch():
    """`[PATCH]` with no N/M is a single patch outside any series;
    not in scope for cross-revision matching."""
    assert parse_in_series_patch_subject("[PATCH] foo: do bar") is None
    assert parse_in_series_patch_subject("[PATCH v2] foo: do bar") is None


def test_parse_in_series_patch_subject_rejects_reply():
    """Replies anchor on `Re:` and the bracket isn't at column 0;
    parser must not treat them as patches."""
    assert parse_in_series_patch_subject("Re: [PATCH v2 1/3] foo: do bar") is None


def test_parse_in_series_patch_subject_rejects_non_patch_bracket():
    """A `[GIT PULL 1/3]` subject would have a valid N/M shape but
    doesn't carry the PATCH token; must not match."""
    assert parse_in_series_patch_subject("[GIT PULL 1/3] foo") is None
    assert parse_in_series_patch_subject("[ANNOUNCE 1/3] foo") is None


def test_parse_in_series_patch_subject_handles_high_positions():
    p = parse_in_series_patch_subject("[PATCH v3 12/20] foo: bar")
    assert p is not None
    assert (p.position, p.total) == (12, 20)


def test_parse_in_series_patch_subject_rejects_empty_or_none():
    assert parse_in_series_patch_subject(None) is None
    assert parse_in_series_patch_subject("") is None


def test_parse_in_series_patch_subject_handles_zero_padded():
    """`git format-patch --numbered` column-aligns positions when
    the series has >=10 patches: `01/27`, `09/27`, `27/27`. The
    leading zeros must not defeat the matcher and the captured
    position must be the actual integer (1, 9), not the padded
    string. Regression for #247."""
    p = parse_in_series_patch_subject("[PATCH v3 01/27] foo: do bar")
    assert p is not None
    assert (p.position, p.total) == (1, 27)
    p = parse_in_series_patch_subject("[PATCH v3 09/27] foo: do bar")
    assert p is not None
    assert (p.position, p.total) == (9, 27)
    # Two-digit position still works (no leading zero needed).
    p = parse_in_series_patch_subject("[PATCH v3 12/27] foo: do bar")
    assert p is not None
    assert (p.position, p.total) == (12, 27)
    # Three-digit padding (rare but valid; >=100-patch series).
    p = parse_in_series_patch_subject("[PATCH v2 007/100] foo: do bar")
    assert p is not None
    assert (p.position, p.total) == (7, 100)


def test_parse_in_series_patch_subject_zero_padded_cover_still_rejected():
    """A zero-padded cover letter shape (`00/N`, `000/N`) still
    isn't an in-series patch; that's `parse_cover_letter`'s job.
    Regression for #247: keep the rejection contract while we relax
    the leading-zero rule on the matcher side."""
    assert parse_in_series_patch_subject("[PATCH v3 00/27] cover") is None
    assert parse_in_series_patch_subject("[PATCH v2 000/100] cover") is None


# --- match_revision_position (DB-free; constructs InSeriesPatch directly) ---


def _mock_patch(
    position: int, subject: str, paths: tuple[str, ...] = ()
) -> InSeriesPatch:
    """Build an InSeriesPatch for matcher tests without DB rows.
    `article` is None because the matcher only reads
    canonical_subject and touched_paths."""
    return InSeriesPatch(
        position=position,
        article=None,  # type: ignore[arg-type]
        canonical_subject=subject,
        touched_paths=frozenset(paths),
    )


def test_match_revision_position_cover_letter_is_trivial():
    v1 = [_mock_patch(0, "series title")]
    v2 = [_mock_patch(0, "series title")]
    result = match_revision_position(v1, v2, 0)
    assert isinstance(result, tuple)
    assert result[0].position == 0 and result[1].position == 0


def test_match_revision_position_cover_letter_titles_can_differ():
    """v1 and v2 cover letters have different titles
    (the maintainer renamed the series). The `patch_series_key`
    upstream already pairs them; the matcher trusts that."""
    v1 = [_mock_patch(0, "v1 title")]
    v2 = [_mock_patch(0, "v2 title")]
    result = match_revision_position(v1, v2, 0)
    assert isinstance(result, tuple)


def test_match_revision_position_by_canonical_subject():
    """Modal case: in-series patches keep the same title across
    revisions; matched by subject equality."""
    v1 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "foo: do bar"),
        _mock_patch(2, "baz: fix bug"),
    ]
    v2 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "foo: do bar"),
        _mock_patch(2, "baz: fix bug"),
    ]
    result = match_revision_position(v1, v2, 1)
    assert isinstance(result, tuple)
    assert result[1].canonical_subject == "foo: do bar"


def test_match_revision_position_subject_match_survives_position_shift():
    """v2 dropped patch 1, so v1's pos=2 (subject `baz: fix bug`)
    now lives at v2's pos=1. Subject-based match catches it; the
    position shift is invisible to the matcher."""
    v1 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "foo: do bar"),
        _mock_patch(2, "baz: fix bug"),
    ]
    v2 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "baz: fix bug"),
    ]
    result = match_revision_position(v1, v2, 2)
    assert isinstance(result, tuple)
    assert result[1].position == 1
    assert result[1].canonical_subject == "baz: fix bug"


def test_match_revision_position_subject_match_is_case_insensitive():
    """Case + whitespace drift between revisions doesn't break the
    match; canonical comparison normalises both sides."""
    v1 = [_mock_patch(0, "cover"), _mock_patch(1, "Foo: Do Bar")]
    v2 = [_mock_patch(0, "cover"), _mock_patch(1, "foo:  do bar")]
    result = match_revision_position(v1, v2, 1)
    assert isinstance(result, tuple)


def test_match_revision_position_file_overlap_fallback():
    """When v2's subject changed (rewrite), match by file overlap.
    v1's patch touches fs/foo.c; v2's third candidate also touches
    fs/foo.c with no other overlap-1+ candidate. That's a confident
    pick."""
    v1 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "old subject", paths=("fs/foo.c",)),
    ]
    v2 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "unrelated A", paths=("fs/other1.c",)),
        _mock_patch(2, "unrelated B", paths=("fs/other2.c",)),
        _mock_patch(3, "new subject", paths=("fs/foo.c",)),
    ]
    result = match_revision_position(v1, v2, 1)
    assert isinstance(result, tuple)
    assert result[1].position == 3


def test_match_revision_position_returns_none_when_v1_missing():
    """`pos` doesn't exist in v1 at all (caller asked about a
    position the series never had)."""
    v1 = [_mock_patch(0, "cover"), _mock_patch(1, "x")]
    v2 = [_mock_patch(0, "cover"), _mock_patch(1, "x")]
    assert match_revision_position(v1, v2, 5) is None


def test_match_revision_position_returns_none_on_no_subject_no_overlap():
    """v2's candidates have neither matching subject nor touched-file
    overlap with v1. Route 404s with `couldn't pick`."""
    v1 = [_mock_patch(0, "cover"), _mock_patch(1, "old", paths=("fs/a.c",))]
    v2 = [_mock_patch(0, "cover"), _mock_patch(1, "new", paths=("fs/b.c",))]
    assert match_revision_position(v1, v2, 1) is None


def test_match_revision_position_returns_ambiguous_on_tied_overlap():
    """Two v2 candidates tie at the same nonzero file-overlap count.
    Matcher refuses to guess; returns AMBIGUOUS_MATCH so the route
    can render `couldn't pick which v2 patch corresponds`."""
    v1 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "old", paths=("fs/a.c", "fs/b.c")),
    ]
    v2 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "new A", paths=("fs/a.c",)),
        _mock_patch(2, "new B", paths=("fs/b.c",)),
    ]
    assert match_revision_position(v1, v2, 1) is AMBIGUOUS_MATCH


def test_match_revision_position_higher_overlap_wins_over_lower():
    """Three v2 candidates with overlap counts 2 / 1 / 0; pick the 2."""
    v1 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "old", paths=("fs/a.c", "fs/b.c", "fs/c.c")),
    ]
    v2 = [
        _mock_patch(0, "cover"),
        _mock_patch(1, "x", paths=("fs/a.c",)),  # overlap 1
        _mock_patch(2, "y", paths=("fs/a.c", "fs/b.c")),  # overlap 2
        _mock_patch(3, "z", paths=("fs/unrelated.c",)),  # overlap 0
    ]
    result = match_revision_position(v1, v2, 1)
    assert isinstance(result, tuple)
    assert result[1].position == 2


# --- resolve_series_patches (integration; uses seeded DB) ---


def _make_article(s, message_id, subject, thread_parent=None, paths=()):
    """Build + add an Article on the alpha inbox with optional
    files; returns the persisted Article."""
    alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    art = Article(
        message_id=message_id,
        subject=subject,
        author="Maintainer <m@example.com>",
        date=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
        thread_parent=thread_parent,
        subject_normalized=subject.lower(),
        lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
        files=[ArticleFile(path=p) for p in paths],
    )
    s.add(art)
    s.flush()
    return art


def test_resolve_series_patches_walks_cover_letter_children(seeded_db):
    """Cover letter's direct thread children that parse as
    `[PATCH ... N/M]` get returned in position order; the cover
    letter itself sits at position 0."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        cover = _make_article(
            s,
            "v1-cover@x",
            "[PATCH 0/3] series title",
            paths=(),
        )
        _make_article(
            s,
            "v1-1@x",
            "[PATCH 1/3] foo: do bar",
            thread_parent=cover.message_id,
            paths=("fs/foo.c",),
        )
        _make_article(
            s,
            "v1-2@x",
            "[PATCH 2/3] baz: fix bug",
            thread_parent=cover.message_id,
            paths=("fs/baz.c",),
        )
        _make_article(
            s,
            "v1-3@x",
            "[PATCH 3/3] qux: tweak",
            thread_parent=cover.message_id,
            paths=("fs/qux.c",),
        )
        s.commit()
        result = resolve_series_patches(s, alpha, cover)
    assert [p.position for p in result] == [0, 1, 2, 3]
    assert result[1].canonical_subject == "foo: do bar"
    assert result[2].touched_paths == frozenset({"fs/baz.c"})


def test_resolve_series_patches_filters_review_replies(seeded_db):
    """Children that DON'T match `[PATCH ... N/M]` (review replies,
    `Re:` etc.) are filtered out. Only in-series patches survive."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        cover = _make_article(s, "cv@x", "[PATCH 0/2] series", paths=())
        _make_article(
            s,
            "p1@x",
            "[PATCH 1/2] foo: do bar",
            thread_parent=cover.message_id,
            paths=("fs/foo.c",),
        )
        _make_article(
            s,
            "p2@x",
            "[PATCH 2/2] baz: fix",
            thread_parent=cover.message_id,
            paths=("fs/baz.c",),
        )
        _make_article(
            s,
            "r1@x",
            "Re: [PATCH 1/2] foo: do bar",
            thread_parent=cover.message_id,
        )
        _make_article(
            s,
            "r2@x",
            "Re: [PATCH 0/2] series",
            thread_parent=cover.message_id,
        )
        s.commit()
        result = resolve_series_patches(s, alpha, cover)
    assert [p.position for p in result] == [0, 1, 2]


# --- compute_revision_diff ---


def test_compute_revision_diff_identical_bodies_marks_is_identical():
    """Byte-identical bodies set `is_identical=True` and leave
    `diff_html=""`; the template uses this to render "No changes
    between v1 and v2 of this patch" instead of an empty diff."""
    body = "commit message\n---\n diff --git a/x b/x\n@@\n+ line\n"
    rd = compute_revision_diff(
        body,
        body,
        from_article_id=1,
        to_article_id=2,
        from_version="v1",
        to_version="v2",
        from_url="/alpha/2024/06/1",
        to_url="/alpha/2024/06/2",
        position=1,
    )
    assert rd.is_identical is True
    assert rd.diff_html == ""


def test_compute_revision_diff_renders_when_bodies_differ():
    """Differing bodies produce non-empty Pygments output;
    `is_identical=False`."""
    rd = compute_revision_diff(
        "old line\nanother\n",
        "new line\nanother\n",
        from_article_id=1,
        to_article_id=2,
        from_version="v1",
        to_version="v2",
        from_url="/alpha/2024/06/1",
        to_url="/alpha/2024/06/2",
        position=1,
    )
    assert rd.is_identical is False
    assert rd.diff_html != ""
    # Pygments emits its DiffLexer-classed spans; one of them
    # should appear in the rendered output. Check loosely so
    # Pygments version drift doesn't break the test.
    assert "highlight" in rd.diff_html  # the cssclass we set
    assert "old line" in rd.diff_html
    assert "new line" in rd.diff_html


def test_compute_revision_diff_handles_none_bodies():
    """One body unavailable from the mirror normalises to empty;
    the diff renders the other side as wholly new/removed without
    crashing."""
    rd = compute_revision_diff(
        None,
        "some content\n",
        from_article_id=1,
        to_article_id=2,
        from_version="v1",
        to_version="v2",
        from_url="/alpha/2024/06/1",
        to_url="/alpha/2024/06/2",
        position=0,
    )
    assert rd.is_identical is False
    assert "some content" in rd.diff_html


def test_compute_revision_diff_preserves_metadata_round_trip():
    """All metadata fields land on the returned dataclass unchanged
    so the cache + template can read them back."""
    rd = compute_revision_diff(
        "a",
        "b",
        from_article_id=42,
        to_article_id=99,
        from_version="v1",
        to_version="v3",
        from_url="/alpha/2024/06/42",
        to_url="/alpha/2024/06/99",
        position=2,
    )
    assert rd.from_article_id == 42
    assert rd.to_article_id == 99
    assert rd.from_version == "v1"
    assert rd.to_version == "v3"
    assert rd.position == 2


def test_resolve_series_patches_scoped_to_inbox(seeded_db):
    """A child article that lives only on `beta` (cross-inbox
    thread spillover) doesn't appear when resolving against
    `alpha`. The article_lists join is the scope guard."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        cover = _make_article(s, "cv@x", "[PATCH 0/2] series")
        _make_article(
            s,
            "p1@x",
            "[PATCH 1/2] foo: do bar",
            thread_parent=cover.message_id,
            paths=("fs/foo.c",),
        )
        # p2 lives only on beta, NOT on alpha.
        beta_only = Article(
            message_id="p2@x",
            subject="[PATCH 2/2] baz: fix",
            author="Other <o@x>",
            date=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=cover.message_id,
            subject_normalized="[patch 2/2] baz: fix",
            lists=[ArticleList(inbox_id=beta.id, epoch="0.git", commit_sha="bb" * 20)],
            files=[ArticleFile(path="fs/baz.c")],
        )
        s.add(beta_only)
        s.commit()
        result_alpha = resolve_series_patches(s, alpha, cover)
        result_beta = resolve_series_patches(s, beta, cover)
    # cover is linked to alpha (via _make_article); beta resolver sees
    # only the beta-linked child p2.
    assert [p.position for p in result_alpha] == [0, 1]
    assert [p.position for p in result_beta] == [0, 2]
