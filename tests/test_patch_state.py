"""Unit tests for `mimir.patch_state.patch_state_for_article`: the
helper feeding the message-page state card (#208)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    MainlineCommit,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)
from mimir.patch_state import (
    PatchState,
    StateMainlineLanding,
    StateSeriesEntry,
    StateTrailerCount,
    _is_patch_subject,
    patch_state_for_article,
)


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
        message_id=message_id, subject=subject,
        author="Maintainer <m@example.com>",
        date=date or datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
        thread_parent=None,
        subject_normalized=subject.lower(),
        lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
        files=[ArticleFile(path=p) for p in paths],
        trailers=[
            ArticleTrailer(
                role=role, name=name, address=addr,
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
            s, art,
            thread_dates=[art.date],
            subsystem_ids=[],
            inbox_name="alpha",
            force=True,
        )
    assert state.is_patch is False
    assert state.trailers == []
    assert state.mainline_landings == []
    assert state.series == []
    assert state.days_since_last_reply is None


def test_patch_state_trailer_roll_up_groups_by_role(seeded_db):
    """Multiple trailers of the same role get bucketed; counts come
    out in `INDEXED_TRAILER_ROLES` order, not insertion order."""
    with seeded_db() as s:
        art = _add_alpha_article(
            s, "rollup@x", "[PATCH 1/2] foo: do bar",
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
            s, art,
            thread_dates=[art.date], subsystem_ids=[], inbox_name="alpha",
            force=True,
        )
    assert state.is_patch is True
    counts = {t.role: t.total for t in state.trailers}
    assert counts == {"Reviewed-by": 3, "Acked-by": 1, "Tested-by": 2}
    # Ordering follows the canonical role tuple, not insertion order.
    assert [t.role for t in state.trailers] == [
        "Reviewed-by", "Acked-by", "Tested-by",
    ]


def test_patch_state_trailer_marks_maintainer_attestations(seeded_db):
    """When a trailer's address matches an M:/R: address on a
    subsystem the patch touches, the row's `maintainer_count`
    reflects that subset."""
    with seeded_db() as s:
        sub = Subsystem(name="FOOBAR", status="Supported")
        sub.paths.append(SubsystemPath(glob="fs/foo/", is_exclude=False))
        sub.maintainers.append(SubsystemMaintainer(
            role="M", name="Alice", address="alice@example.com",
        ))
        sub.maintainers.append(SubsystemMaintainer(
            role="R", name="Bob", address="bob@example.com",
        ))
        s.add(sub)
        s.flush()
        art = _add_alpha_article(
            s, "maint@x", "[PATCH 1/2] foo: fix it",
            trailers=[
                ("Reviewed-by", "Alice", "alice@example.com"),  # M:
                ("Reviewed-by", "Bob", "bob@example.com"),      # R:
                ("Reviewed-by", "Carol", "carol@example.com"),  # random
                ("Acked-by", "Dave", "dave@example.com"),       # random
            ],
            paths=("fs/foo/file.c",),
        )
        s.commit()
        state = patch_state_for_article(
            s, art,
            thread_dates=[art.date],
            subsystem_ids=[sub.id], inbox_name="alpha",
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
            s, art,
            thread_dates=[art.date],
            subsystem_ids=[], inbox_name="alpha",
            force=True,
        )
    assert state.trailers == []


def test_patch_state_surfaces_mainline_landing(seeded_db):
    """A `mainline_commits` row referencing this article's
    message_id surfaces in `mainline_landings`."""
    with seeded_db() as s:
        art = _add_alpha_article(s, "landed@x", "[PATCH 1/2] foo: land")
        s.add(MainlineCommit(
            commit_sha="deadbeef" * 5,
            tree_name="linus",
            message_id="landed@x",
            committed_at=datetime(2024, 7, 1, 0, 0, tzinfo=timezone.utc),
        ))
        s.commit()
        state = patch_state_for_article(
            s, art,
            thread_dates=[art.date], subsystem_ids=[], inbox_name="alpha",
            force=True,
        )
    assert len(state.mainline_landings) == 1
    landing = state.mainline_landings[0]
    assert isinstance(landing, StateMainlineLanding)
    assert landing.commit_sha == "deadbeef" * 5
    assert landing.tree_name == "linus"
    assert landing.committed_at.date() == datetime(2024, 7, 1).date()


def test_patch_state_landing_row_empty_when_not_landed(seeded_db):
    """No mainline_commits row → empty `mainline_landings`. The
    template skips the row rather than rendering "Not in mainline"
    (deferred until mainline_state grows a timestamp column)."""
    with seeded_db() as s:
        art = _add_alpha_article(s, "in-flight@x", "[PATCH 1/2] foo: WIP")
        s.commit()
        state = patch_state_for_article(
            s, art,
            thread_dates=[art.date], subsystem_ids=[], inbox_name="alpha",
            force=True,
        )
    assert state.mainline_landings == []


def test_patch_state_series_timeline_with_diff_links(seeded_db):
    """Cover letters with 2+ revisions get a timeline; each non-
    current revision carries a `diff_url` pointing at the #210
    inter-revision-diff route."""
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
                lists=[ArticleList(
                    inbox_id=alpha.id, epoch="0.git", commit_sha="bb" * 20,
                )],
            )

        v1 = _cover("v1-cv@x", "v1", datetime(2024, 6, 1, tzinfo=timezone.utc))
        # v2 doesn't get the `v2 ` infix in version-strip; the parser
        # accepts both `[PATCH 0/2]` (v1 implicit) and `[PATCH v2 0/2]`.
        v2 = Article(
            message_id="v2-cv@x",
            subject="[PATCH v2 0/2] common series title",
            author="Author <a@example.com>",
            date=datetime(2024, 6, 15, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="[patch v2 0/2] common series title",
            patch_series_key="testkeyseries",
            patch_series_version="v2",
            lists=[ArticleList(
                inbox_id=alpha.id, epoch="0.git", commit_sha="cc" * 20,
            )],
        )
        # v1 also needs a series_key + version; the helper here writes
        # them since we're bypassing ingest.
        v1.patch_series_key = "testkeyseries"
        v1.patch_series_version = "v1"
        s.add_all([v1, v2])
        s.commit()
        # Pretend we're viewing v2 (the latest revision).
        state = patch_state_for_article(
            s, v2,
            thread_dates=[v2.date],
            subsystem_ids=[], inbox_name="alpha",
            force=True,
        )
    versions = [e.version for e in state.series]
    assert versions == ["v1", "v2"]
    v1_entry = next(e for e in state.series if e.version == "v1")
    v2_entry = next(e for e in state.series if e.version == "v2")
    assert v2_entry.is_current is True
    assert v2_entry.diff_url is None  # no diff for the current revision
    assert v1_entry.is_current is False
    assert v1_entry.diff_url is not None
    assert "/alpha/series/testkeyseries/diff" in v1_entry.diff_url
    assert "from=v1" in v1_entry.diff_url
    assert "to=v2" in v1_entry.diff_url
    assert "pos=cover" in v1_entry.diff_url


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
                message_id=mid, subject=subj, author="A <a@x>",
                date=datetime(2024, 6, day, tzinfo=timezone.utc),
                thread_parent=None,
                subject_normalized=subj.lower(),
                patch_series_key="sk",
                patch_series_version=ver,
                patch_series_position=pos,
                lists=[ArticleList(
                    inbox_id=alpha.id, epoch="0.git",
                    commit_sha=str(day).zfill(2) * 20,
                )],
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
            s, v2_1of3,
            thread_dates=[v2_1of3.date],
            subsystem_ids=[], inbox_name="alpha",
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
    v1_entry = next(e for e in state.series if e.version == "v1")
    assert v1_entry.diff_url is not None
    assert "pos=1" in v1_entry.diff_url
    assert "pos=cover" not in v1_entry.diff_url


def test_patch_state_series_empty_for_solo_revision(seeded_db):
    """A cover letter that's the only revision of its series renders
    no timeline (the row would just say `v1` which is noise)."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        v1 = Article(
            message_id="solo-cv@x",
            subject="[PATCH 0/2] lonely cover",
            author="A <a@x>",
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="[patch 0/2] lonely cover",
            patch_series_key="solo",
            patch_series_version="v1",
            lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="ee" * 20)],
        )
        s.add(v1)
        s.commit()
        state = patch_state_for_article(
            s, v1,
            thread_dates=[v1.date],
            subsystem_ids=[], inbox_name="alpha",
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
            s, art,
            thread_dates=[art.date],
            subsystem_ids=[], inbox_name="alpha",
            force=True,
        )
    assert state.days_since_last_reply is None


def test_patch_state_activity_computes_days_since_last_reply(seeded_db):
    """`days_since_last_reply` is the integer days from `now()` to
    the max thread date that's NEWER than the article itself."""
    with seeded_db() as s:
        post_date = datetime.now(timezone.utc) - timedelta(days=10)
        reply_date = datetime.now(timezone.utc) - timedelta(days=3)
        art = _add_alpha_article(
            s, "with-replies@x", "[PATCH 1/2] foo: do bar",
            date=post_date,
        )
        s.commit()
        state = patch_state_for_article(
            s, art,
            thread_dates=[post_date, reply_date],
            subsystem_ids=[], inbox_name="alpha",
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
            s, "cached@x", "[PATCH 1/2] foo: do bar",
            trailers=[("Reviewed-by", "A", "a@example.com")],
        )
        s.commit()
        first = patch_state_for_article(
            s, art,
            thread_dates=[art.date], subsystem_ids=[], inbox_name="alpha",
        )
        second = patch_state_for_article(
            s, art,
            thread_dates=[art.date], subsystem_ids=[], inbox_name="alpha",
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
    assert dataclasses.is_dataclass(StateMainlineLanding)
    assert dataclasses.is_dataclass(StateSeriesEntry)
    assert dataclasses.is_dataclass(PatchState)
