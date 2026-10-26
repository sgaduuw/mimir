"""Unit tests for `mimir.patch_state.patch_state_for_article`: the
helper feeding the message-page state card (#208)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)
from mimir.patch_state import (
    PatchState,
    StateSeriesEntry,
    StateTrailerCount,
    _is_patch_subject,
    patch_state_for_article,
)


def _seed_article(session, message_id):
    """Seed a minimal patch article on the alpha inbox. Returns the
    persisted Article. Uses the live session directly so callers can
    add further rows (e.g. MainlineCommit) to the same transaction."""
    alpha = session.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    art = Article(
        message_id=message_id,
        subject="[PATCH 1/2] foo: do bar",
        author="Author <a@example.com>",
        date=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        thread_parent=None,
        subject_normalized="[patch 1/2] foo: do bar",
        lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="fa" * 20)],
    )
    session.add(art)
    session.flush()
    return art


# --- _is_patch_subject (pure function; no DB) ---


def test_is_patch_subject_recognises_cover_letter():
    assert _is_patch_subject("[PATCH 0/3] series title") is True
    assert _is_patch_subject("[PATCH v2 0/3] series title") is True


def test_is_patch_subject_recognises_in_series_patch():
    assert _is_patch_subject("[PATCH 1/3] foo: do bar") is True
    assert _is_patch_subject("[PATCH v2 2/3] foo: do bar") is True


def test_is_patch_subject_recognises_solo_patch():
    """Single-patch posts (no N/M) aren't matched by the cover-letter
    or in-series parsers but ARE patches the state card belongs on."""
    assert _is_patch_subject("[PATCH] foo: do bar") is True
    assert _is_patch_subject("[PATCH v2] foo: do bar") is True
    assert _is_patch_subject("[PATCH net-next] foo: do bar") is True


def test_is_patch_subject_recognises_list_tagged_solo_patch():
    """A list tag before a solo PATCH bracket is still a patch (#711):
    the card follows the same rule as the series parsers."""
    assert _is_patch_subject("[U-Boot] [PATCH] fix: do bar") is True
    assert _is_patch_subject("[Qemu-devel] [PATCH v2] fix: do bar") is True
    assert _is_patch_subject("[patch] lower-case token") is True


@pytest.mark.parametrize(
    "subject",
    [
        "[PATCH] solo",
        "[U-Boot] [PATCH] solo",
        "[U-Boot] [PATCH 0/3] series",
        "[net-next v7 02/10] x",
        "[patch v2] lower",
        "[GIT PULL] fixes",
        "[RFC] discussion",
        "[linux-next:master 1000/2056] report",
        "Re: [PATCH] x",
        "plain prose",
    ],
)
def test_is_patch_subject_agrees_with_patch_bracket(subject):
    """One predicate decides what a patch is (AGENTS.md): the state card
    shows exactly where `patch_bracket` finds a patch bracket."""
    from mimir.patch_revisions import patch_bracket

    assert _is_patch_subject(subject) is (patch_bracket(subject) is not None)


def test_is_patch_subject_rejects_reply():
    """Replies have `Re: [PATCH ...]` so the bracket isn't at column 0;
    the card shouldn't render on review-discussion pages."""
    assert _is_patch_subject("Re: [PATCH v2 1/3] foo: do bar") is False
    assert _is_patch_subject("Re: [PATCH] foo") is False


def test_is_patch_subject_rejects_non_patch_brackets():
    assert _is_patch_subject("[GIT PULL] urgent fixes") is False
    assert _is_patch_subject("[ANNOUNCE] foo 6.0 released") is False
    assert _is_patch_subject("[RFC] some discussion") is False


def test_is_patch_subject_rejects_prose():
    assert _is_patch_subject("just an email about stuff") is False
    assert _is_patch_subject("") is False
    assert _is_patch_subject(None) is False


# --- patch_state_for_article (integration; uses seeded DB) ---


def _add_alpha_article(s, message_id, subject, *, trailers=(), date=None, paths=()):
    """Seed one article on alpha with optional trailer + path rows.
    Returns the persisted Article."""
    alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    art = Article(
        message_id=message_id,
        subject=subject,
        author="Maintainer <m@example.com>",
        date=date or datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        thread_parent=None,
        subject_normalized=subject.lower(),
        # A stored in-series patch has its position; NULL marks a reply (#838).
        patch_series_position=1,
        lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
        files=[ArticleFile(path=p) for p in paths],
        trailers=[
            ArticleTrailer(
                role=role,
                name=name,
                address=addr,
                address_normalized=addr.lower(),
            )
            for role, name, addr in trailers
        ],
    )
    s.add(art)
    s.flush()
    return art


def test_patch_state_for_non_patch_returns_empty(seeded_db):
    """Articles whose subject isn't a patch shape get
    `is_patch=False`; the template skips the card entirely."""
    with seeded_db() as s:
        art = _add_alpha_article(s, "non-patch@x", "just a discussion")
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert state.is_patch is False
    assert state.trailers == []
    assert state.series == []
    assert state.days_since_last_reply is None


@pytest.mark.parametrize(
    ("subject", "position", "expected"),
    [
        # #838: ingest and the reprocess clear position and key on a reply
        # that repeats a patch or cover subject, so NULL position and key
        # (the seeded key is NULL) means reply.
        ("[PATCH 1/3] foo: bar", None, False),
        ("[PATCH 0/3] foo series", None, False),
        ("[PATCH 1/3] foo: bar", 1, True),
        ("[PATCH 0/3] foo series", 0, True),
        # A solo patch never has a position; the subject decides.
        ("[PATCH] foo: bar", None, True),
    ],
)
def test_patch_state_card_follows_stored_position(
    seeded_db, subject, position, expected
):
    with seeded_db() as s:
        art = _add_alpha_article(s, "card@x", subject)
        art.patch_series_position = position
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert state.is_patch is expected


def test_patch_state_trailer_roll_up_groups_by_role(seeded_db):
    """Multiple trailers of the same role get bucketed; counts come
    out in `INDEXED_TRAILER_ROLES` order, not insertion order."""
    with seeded_db() as s:
        art = _add_alpha_article(
            s,
            "rollup@x",
            "[PATCH 1/2] foo: do bar",
            trailers=[
                ("Reviewed-by", "Alice", "alice@example.com"),
                ("Reviewed-by", "Bob", "bob@example.com"),
                ("Reviewed-by", "Carol", "carol@example.com"),
                ("Acked-by", "Dave", "dave@example.com"),
                ("Tested-by", "Eve", "eve@example.com"),
                ("Tested-by", "Frank", "frank@example.com"),
            ],
        )
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert state.is_patch is True
    counts = {t.role: t.total for t in state.trailers}
    assert counts == {"Reviewed-by": 3, "Acked-by": 1, "Tested-by": 2}
    # Ordering follows the canonical role tuple, not insertion order.
    assert [t.role for t in state.trailers] == [
        "Reviewed-by",
        "Acked-by",
        "Tested-by",
    ]


def test_patch_state_trailer_marks_maintainer_attestations(seeded_db):
    """When a trailer's address matches an M:/R: address on a
    subsystem the patch touches, the row's `maintainer_count`
    reflects that subset."""
    with seeded_db() as s:
        sub = Subsystem(name="FOOBAR", status="Supported")
        sub.paths.append(SubsystemPath(glob="fs/foo/", is_exclude=False))
        sub.maintainers.append(
            SubsystemMaintainer(
                role="M",
                name="Alice",
                address="alice@example.com",
            )
        )
        sub.maintainers.append(
            SubsystemMaintainer(
                role="R",
                name="Bob",
                address="bob@example.com",
            )
        )
        s.add(sub)
        s.flush()
        art = _add_alpha_article(
            s,
            "maint@x",
            "[PATCH 1/2] foo: fix it",
            trailers=[
                ("Reviewed-by", "Alice", "alice@example.com"),  # M:
                ("Reviewed-by", "Bob", "bob@example.com"),  # R:
                ("Reviewed-by", "Carol", "carol@example.com"),  # random
                ("Acked-by", "Dave", "dave@example.com"),  # random
            ],
            paths=("fs/foo/file.c",),
        )
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[sub.id],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    rev = next(t for t in state.trailers if t.role == "Reviewed-by")
    assert rev.total == 3
    assert rev.maintainer_count == 2  # Alice (M) + Bob (R), not Carol
    ack = next(t for t in state.trailers if t.role == "Acked-by")
    assert ack.total == 1
    assert ack.maintainer_count == 0


def test_patch_state_no_trailers_no_subsystems_skips_row(seeded_db):
    """A trailerless patch with no subsystem hits leaves the trailers
    row empty; template renders no roll-up line."""
    with seeded_db() as s:
        art = _add_alpha_article(s, "naked@x", "[PATCH 1/2] foo: do bar")
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert state.trailers == []


def test_patch_state_series_timeline_with_diff_links(seeded_db):
    """Cover letters with 2+ revisions get a timeline; each revision
    after the first carries a `diff_url` from its previous revision,
    pointing at the #210 inter-revision-diff route (#844)."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

        def _cover(msgid, version, date):
            return Article(
                message_id=msgid,
                subject=f"[PATCH {version} 0/2] common series title",
                author="Author <a@example.com>",
                date=date,
                thread_parent=None,
                subject_normalized=f"[patch {version} 0/2] common series title",
                patch_series_key="testkeyseries",
                patch_series_version=version,
                lists=[
                    ArticleList(
                        inbox_id=alpha.id,
                        epoch="0.git",
                        commit_sha="bb" * 20,
                    )
                ],
            )

        v1 = _cover("v1-cv@x", "v1", datetime(2024, 6, 1, tzinfo=UTC))
        # v2 doesn't get the `v2 ` infix in version-strip; the parser
        # accepts both `[PATCH 0/2]` (v1 implicit) and `[PATCH v2 0/2]`.
        v2 = Article(
            message_id="v2-cv@x",
            subject="[PATCH v2 0/2] common series title",
            author="Author <a@example.com>",
            date=datetime(2024, 6, 15, tzinfo=UTC),
            thread_parent=None,
            subject_normalized="[patch v2 0/2] common series title",
            patch_series_key="testkeyseries",
            patch_series_version="v2",
            lists=[
                ArticleList(
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="cc" * 20,
                )
            ],
        )
        # v1 also needs a series_key + version; the helper here writes
        # them since we're bypassing ingest.
        v1.patch_series_key = "testkeyseries"
        v1.patch_series_version = "v1"
        s.add_all([v1, v2])
        s.commit()
        # Pretend we're viewing v2 (the latest revision).
        state = patch_state_for_article(
            s,
            v2,
            thread_dates=[v2.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    versions = [e.version for e in state.series]
    assert versions == ["v1", "v2"]
    v1_entry = next(e for e in state.series if e.version == "v1")
    v2_entry = next(e for e in state.series if e.version == "v2")
    assert v1_entry.is_current is False
    assert v1_entry.diff_url is None  # nothing before the first revision
    assert v2_entry.is_current is True
    assert v2_entry.diff_url is not None
    assert "/alpha/series/testkeyseries/diff" in v2_entry.diff_url
    assert "from=v1" in v2_entry.diff_url
    assert "to=v2" in v2_entry.diff_url
    assert "pos=cover" in v2_entry.diff_url


def _slot_row(msgid, version, day, *, position=0, key="slotkey", diff=False):
    """One series row, a cover by default; `diff` gives it an
    `article_files` row, the has-a-diff predicate of the slot rule."""
    subject = (
        f"[PATCH {version} 0/2] common series title"
        if position == 0
        else f"[PATCH {version} {position}/2] foo: bar"
    )
    art = Article(
        message_id=msgid,
        subject=subject,
        author="Author <a@example.com>",
        date=datetime(2024, 6, day, tzinfo=UTC),
        thread_parent=None,
        subject_normalized=subject.lower(),
        patch_series_key=key,
        patch_series_version=version,
        patch_series_position=position,
    )
    if diff:
        art.files = [ArticleFile(path="fs/foo.c")]
    return art


def _seed_slot_rows(s, rows):
    """Add `rows` to `s` filed in alpha, commit, and return them in
    order (ids follow it)."""
    alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    for r in rows:
        r.lists = [ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="bb" * 20)]
    s.add_all(rows)
    s.commit()
    return rows


def _card(s, article):
    return patch_state_for_article(
        s,
        article,
        thread_dates=[article.date],
        subsystem_ids=[],
        inbox_name="alpha",
        state_key="t",
        force=True,
    )


def test_patch_state_series_timeline_folds_a_resend_into_its_version(seeded_db):
    """#661, #861: a resend carries the same version string as an earlier
    revision. One entry per version: the slot's patch, with the resend
    listed under it, and the diff link on the version's entry. Viewed
    from the resend, the version's entry is the current one."""
    with seeded_db() as s:
        v1, v2, v2_resend = _seed_slot_rows(
            s,
            [
                _slot_row("rs-v1@x", "v1", 1),
                _slot_row("rs-v2@x", "v2", 10),
                _slot_row("rs-v2b@x", "v2", 11),
            ],
        )
        state = _card(s, v2_resend)
        ids = (v1.id, v2.id, v2_resend.id)
        # Precondition: v2 and its resend share one slot.
        assert v2.patch_series_version == v2_resend.patch_series_version == "v2"
    assert [(e.version, e.article_id) for e in state.series] == [
        ("v1", ids[0]),
        ("v2", ids[1]),
    ]
    v1_entry, v2_entry = state.series
    assert v1_entry.diff_url is None
    assert "from=v1&to=v2" in v2_entry.diff_url
    assert v1_entry.reposts == []
    assert [(r.article_id, r.is_current) for r in v2_entry.reposts] == [(ids[2], True)]
    assert (v1_entry.is_current, v2_entry.is_current) == (False, True)


def test_patch_state_series_timeline_names_the_row_with_a_diff_the_patch(seeded_db):
    """#861: of two rows in one slot, the lowest id with a diff is the
    patch, not a lower-id row without one."""
    with seeded_db() as s:
        v1, no_diff, with_diff = _seed_slot_rows(
            s,
            [
                _slot_row("sd-v1@x", "v1", 1, position=1, diff=True),
                _slot_row("sd-v2a@x", "v2", 10, position=1),
                _slot_row("sd-v2b@x", "v2", 11, position=1, diff=True),
            ],
        )
        state = _card(s, v1)
        ids = (no_diff.id, with_diff.id)
    v2_entry = state.series[1]
    assert v2_entry.article_id == ids[1]  # not the lower id, which has no diff
    assert [r.article_id for r in v2_entry.reposts] == [ids[0]]
    # The link and the diff page's canonical name the same winner.
    assert "from=v1&to=v2" in v2_entry.diff_url


def test_patch_state_series_timeline_has_no_fold_for_one_version(seeded_db):
    """#861: a version posted twice is still one version, so the fold,
    which renders at two or more, has nothing to show."""
    with seeded_db() as s:
        first, again = _seed_slot_rows(
            s, [_slot_row("one-a@x", "v1", 1), _slot_row("one-b@x", "v1", 2)]
        )
        state = _card(s, again)
    assert state.series == []


def test_patch_synthesis_counts_versions_not_rows(seeded_db):
    """#861: v1, v2 and a resend of v2 are two versions, so the sentence
    says "2 revisions archived", not 3."""
    from mimir.web.filters import _patch_synthesis_filter

    with seeded_db() as s:
        _v1, _v2, resend = _seed_slot_rows(
            s,
            [
                _slot_row("sy-v1@x", "v1", 1),
                _slot_row("sy-v2@x", "v2", 10),
                _slot_row("sy-v2b@x", "v2", 11),
            ],
        )
        state = _card(s, resend)
    assert _patch_synthesis_filter(state) == "Revision v2 (2 revisions archived)."


def test_patch_state_card_skips_a_no_diff_row_that_loses_its_slot(seeded_db):
    """#861: a reply repeating a patch's bracketed subject is a row of
    the patch's slot with no diff. It is not a patch, so no card. The
    row that has the diff, and a re-post with its own diff, still are."""
    with seeded_db() as s:
        patch, reply, repost = _seed_slot_rows(
            s,
            [
                _slot_row("ls-p@x", "v1", 1, position=1, diff=True),
                _slot_row("ls-r@x", "v1", 2, position=1),
                _slot_row("ls-q@x", "v1", 3, position=1, diff=True),
            ],
        )
        cards = [_card(s, a).is_patch for a in (patch, reply, repost)]
    assert cards == [True, False, True]


def test_patch_state_card_keeps_a_slot_where_no_row_has_a_diff(seeded_db):
    """#861: with no diff anywhere, the lowest id is the patch and the
    card stays on every row, as before the rule."""
    with seeded_db() as s:
        rows = _seed_slot_rows(
            s,
            [
                _slot_row("nd-a@x", "v1", 1, position=1),
                _slot_row("nd-b@x", "v1", 2, position=1),
            ],
        )
        cards = [_card(s, a).is_patch for a in rows]
    assert cards == [True, True]


def test_patch_state_series_timeline_renders_on_in_series_patch(seeded_db):
    """#212: series row renders on `[PATCH N/M]` pages too, not
    just covers. Same-position filter narrows to revisions of THIS
    patch position (1/3 across v1 and v2), not every patch in
    every revision."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # Two revisions of `[PATCH 1/3]` plus a sibling `[PATCH 2/3]`
        # under each rev. The timeline for 1/3 must contain only the
        # 1/3 rows, not the 2/3 siblings.
        rows = [
            Article(
                message_id=mid,
                subject=subj,
                author="A <a@x>",
                date=datetime(2024, 6, day, tzinfo=UTC),
                thread_parent=None,
                subject_normalized=subj.lower(),
                patch_series_key="sk",
                patch_series_version=ver,
                patch_series_position=pos,
                lists=[
                    ArticleList(
                        inbox_id=alpha.id,
                        epoch="0.git",
                        commit_sha=str(day).zfill(2) * 20,
                    )
                ],
            )
            for mid, subj, ver, pos, day in [
                ("v1-1of3@x", "[PATCH 1/3] foo: A", "v1", 1, 1),
                ("v1-2of3@x", "[PATCH 2/3] foo: B", "v1", 2, 1),
                ("v2-1of3@x", "[PATCH v2 1/3] foo: A", "v2", 1, 15),
                ("v2-2of3@x", "[PATCH v2 2/3] foo: B", "v2", 2, 15),
            ]
        ]
        s.add_all(rows)
        s.commit()
        v2_1of3 = next(r for r in rows if r.message_id == "v2-1of3@x")
        state = patch_state_for_article(
            s,
            v2_1of3,
            thread_dates=[v2_1of3.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    # Two entries: v1 and v2 of position 1/3. The 2/3 siblings are
    # excluded by the same-position filter.
    assert [e.version for e in state.series] == ["v1", "v2"]
    article_ids = {e.article_id for e in state.series}
    assert article_ids == {
        next(r.id for r in rows if r.message_id == "v1-1of3@x"),
        next(r.id for r in rows if r.message_id == "v2-1of3@x"),
    }
    # Diff URL uses `pos=1` for in-series patches (not `pos=cover`).
    v2_entry = next(e for e in state.series if e.version == "v2")
    assert v2_entry.diff_url is not None
    assert "pos=1" in v2_entry.diff_url
    assert "pos=cover" not in v2_entry.diff_url


def test_patch_state_series_empty_for_solo_revision(seeded_db):
    """A cover letter that's the only revision of its series renders
    no timeline (the row would just say `v1` which is noise)."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        v1 = Article(
            message_id="solo-cv@x",
            subject="[PATCH 0/2] lonely cover",
            author="A <a@x>",
            date=datetime(2024, 6, 1, tzinfo=UTC),
            thread_parent=None,
            subject_normalized="[patch 0/2] lonely cover",
            patch_series_key="solo",
            patch_series_version="v1",
            lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="ee" * 20)],
        )
        s.add(v1)
        s.commit()
        state = patch_state_for_article(
            s,
            v1,
            thread_dates=[v1.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert state.series == []


def test_patch_state_activity_no_replies(seeded_db):
    """A patch with no replies (only itself in the thread) gets
    `days_since_last_reply=None`; template renders "No replies"."""
    with seeded_db() as s:
        art = _add_alpha_article(s, "lonely@x", "[PATCH 1/2] foo: solo")
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert state.days_since_last_reply is None


def test_patch_state_activity_computes_days_since_last_reply(seeded_db):
    """`days_since_last_reply` is the integer days from `now()` to
    the max thread date that's NEWER than the article itself."""
    with seeded_db() as s:
        post_date = datetime.now(UTC) - timedelta(days=10)
        reply_date = datetime.now(UTC) - timedelta(days=3)
        art = _add_alpha_article(
            s,
            "with-replies@x",
            "[PATCH 1/2] foo: do bar",
            date=post_date,
        )
        s.commit()
        state = patch_state_for_article(
            s,
            art,
            thread_dates=[post_date, reply_date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    # Allow ±1 day slack for timing variance crossing midnight UTC
    # during the test run.
    assert state.days_since_last_reply in (2, 3, 4)


def test_patch_state_is_cached(seeded_db):
    """Second invocation hits the cache; force=True bypasses. Pins
    the cache shape so a future encoder change surfaces here."""
    with seeded_db() as s:
        art = _add_alpha_article(
            s,
            "cached@x",
            "[PATCH 1/2] foo: do bar",
            trailers=[("Reviewed-by", "A", "a@example.com")],
        )
        s.commit()
        first = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
        )
        second = patch_state_for_article(
            s,
            art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
        )
    # Same shape; cache round-trip didn't lose information.
    assert first.is_patch == second.is_patch
    assert [(t.role, t.total) for t in first.trailers] == [
        (t.role, t.total) for t in second.trailers
    ]


def test_patch_state_trailer_count_is_serializable_dataclass():
    """Sanity check that the registered nested dataclasses are
    properly typed (the cache encoder iterates `dataclasses.fields`
    and would fail for a non-dataclass)."""
    import dataclasses

    assert dataclasses.is_dataclass(StateTrailerCount)
    assert dataclasses.is_dataclass(StateSeriesEntry)
    assert dataclasses.is_dataclass(PatchState)


def test_resolve_tree_label_uses_the_display_name_or_the_slug():
    """The status line's "Queued in <label>" (#844): Linus's TreeConfig
    sets display_name='mainline (Linus)'; subsystem trees default to
    display_name=None and fall back to the slug."""
    from mimir.patch_state import _resolve_tree_label

    assert _resolve_tree_label("linus") == "mainline (Linus)"
    assert _resolve_tree_label("net-next") == "net-next"


# --- _activity_heat (pure function; no DB) ---


@pytest.mark.parametrize(
    "days,expected",
    [
        (None, ("dormant", "no replies")),
        (0, ("hot", "today")),
        (1, ("warm", "1d")),
        (3, ("warm", "3d")),
        (4, ("cooling", "4d")),
        (14, ("cooling", "14d")),
        (15, ("cold", "15d")),
        (60, ("cold", "60d")),
        (61, ("stale", "61d")),
        (365, ("stale", "365d")),
    ],
)
def test_activity_heat_buckets(days, expected):
    from mimir.patch_state import _activity_heat

    assert _activity_heat(days) == expected


def test_series_timeline_treats_null_position_as_zero(seeded_db):
    """The pill treats a NULL position as 0 (a cover before the #212
    backfill), so the revisions fold must too, or a SUPERSEDED pill
    sits over an empty fold."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

        def _cover(msgid, version, position, day):
            return Article(
                message_id=msgid,
                subject=f"[PATCH {version} 0/2] title",
                author="Author <a@example.com>",
                date=datetime(2024, 6, day, tzinfo=UTC),
                subject_normalized=f"[patch {version} 0/2] title",
                patch_series_key="nullposkey",
                patch_series_version=version,
                patch_series_position=position,
                lists=[
                    ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="dd" * 20)
                ],
            )

        v1 = _cover("nullpos-v1@x", "v1", None, 1)
        v2 = _cover("nullpos-v2@x", "v2", 0, 15)
        s.add_all([v1, v2])
        s.commit()
        state = patch_state_for_article(
            s,
            v1,
            thread_dates=[v1.date],
            subsystem_ids=[],
            inbox_name="alpha",
            state_key="t",
            force=True,
        )
    assert [e.version for e in state.series] == ["v1", "v2"]


def test_a_card_cached_before_reposts_existed_still_decodes():
    """#861 added `reposts` without bumping `cache.NAMESPACE_VERSION`,
    which would orphan every cache. A card cached in the old shape must
    keep decoding, with no re-posts."""
    from mimir.cache import _decode

    old_entry = {
        "__t": "StateSeriesEntry",
        "v": {
            "version": "v1",
            "article_id": 1,
            "date": None,
            "url": "/alpha/2024/06/1",
            "is_current": True,
            "diff_url": None,
        },
    }
    state = _decode(
        {
            "__t": "PatchState",
            "v": {
                "is_patch": True,
                "trailers": [],
                "mainline_landings": [],
                "series": [old_entry],
                "days_since_last_reply": None,
            },
        }
    )
    assert state.series == [
        StateSeriesEntry(
            version="v1",
            article_id=1,
            date=None,
            url="/alpha/2024/06/1",
            is_current=True,
            diff_url=None,
            reposts=[],
        )
    ]
