"""Helper-function contracts for `mimir.store` and `mimir.web`.

The route smoke tests cover the happy-path read of these end-to-
end. This file pins the error paths and the privacy / RFC-6266
edge cases that would never surface from real-data smoke runs.
"""

from sqlalchemy import select

import pytest

from mimir.models import Inbox
from mimir.store import MessageNotFound, read_message
from mimir.web import (
    _canonical_inbox_names_for,
    _content_disposition,
    _redact_trailer_address,
    _safe_from_filter,
)


def _alpha(seeded_db) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


# store.read_message, error paths


def test_read_message_unknown_message_id_raises(seeded_db):
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound):
        read_message(s, alpha, "nonexistent@example.com")


def test_read_message_message_in_other_inbox_raises(seeded_db):
    """art2 is beta-only. Asking alpha for it must 404, even
    though the Message-ID exists in DB."""
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound):
        read_message(s, alpha, "art2@example.com")


def test_read_message_missing_mirror_raises(seeded_db):
    """The seed sets mirror_path=/tmp/alpha which doesn't exist on
    disk. A read against a real Message-ID should raise
    MessageNotFound with the path-not-found message rather than
    crashing trying to open the repo."""
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound, match="not found"):
        read_message(s, alpha, "art1@example.com")


def test_read_message_stale_commit_sha_raises(seeded_db, tmp_path):
    """A real-but-stale commit_sha (mirror got rewritten / blob GC'd)
    must surface as MessageNotFound, not bubble out as a KeyError
    that 500s the message route. dulwich raises KeyError when the
    commit isn't in the object store; _read_blob now catches and
    converts."""
    from dulwich.repo import Repo as DulwichRepo
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox

    # Empty bare repo so the epoch_path.exists() guard passes but
    # the commit_sha lookup inside doesn't.
    epoch_dir = tmp_path / "0.git"
    DulwichRepo.init_bare(str(epoch_dir), mkdir=True)

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(tmp_path)
        # art1 is alpha-only in the seeded DB; the seed left
        # commit_sha = "aa" * 20, which won't be in our empty bare
        # repo. That's the stale-row shape we want to exercise.
        s.execute(
            update(ArticleList)
            .where(ArticleList.inbox_id == ix.id)
            .values(epoch="0.git")
        )
        s.commit()

    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound, match="blob"):
        read_message(s, alpha, "art1@example.com")


# store.read_messages, the bulk sibling


def _count_repo_opens(monkeypatch) -> list:
    """Record every `Repo(...)` construction `mimir.store` performs.

    The saving this helper exists to pin is entirely in the NUMBER of
    opens: dulwich re-reads and re-mmaps the epoch's pack index on
    each one. A test that only asserted the right bodies came back
    would pass against the per-message shape it replaced.
    """
    from dulwich.repo import Repo as RealRepo

    import mimir.store

    opened: list[str] = []

    class _CountingRepo(RealRepo):
        def __init__(self, path, *args, **kwargs):
            opened.append(str(path))
            super().__init__(path, *args, **kwargs)

    monkeypatch.setattr(mimir.store, "Repo", _CountingRepo)
    return opened


def _alpha_live():
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


def test_read_messages_opens_one_repo_for_a_single_epoch_thread(tmp_path, monkeypatch):
    """The whole point of the bulk read. Six messages in one epoch is
    six pack-index reopens on the per-message shape and one here."""
    from mimir.extensions import SessionLocal
    from mimir.store import read_messages
    from tests.test_routes._helpers import seed_thread_shape

    ids = [f"bulk{i}@x" for i in range(6)]
    seed_thread_shape(
        tmp_path, "alpha", [(ids[0], None)] + [(m, ids[0]) for m in ids[1:]]
    )
    alpha = _alpha_live()

    opened = _count_repo_opens(monkeypatch)
    with SessionLocal() as s:
        got = read_messages(s, alpha, ids)

    assert set(got) == set(ids)
    assert len(opened) == 1, f"expected one repo open, got {len(opened)}: {opened}"


def test_read_messages_reads_a_thread_that_straddles_an_epoch_boundary(
    tmp_path, monkeypatch
):
    """public-inbox chunks epochs by SIZE, not by conversation, so a
    thread can span two of them. This is why the grouping is per-epoch
    rather than one shared handle for the whole call: the simpler
    single-handle shape silently drops every message on the far side of
    the boundary."""
    from mimir.extensions import SessionLocal
    from mimir.store import read_messages
    from tests.test_routes._helpers import seed_thread_shape

    seed_thread_shape(tmp_path, "alpha", [("span0@x", None)], epoch="0.git")
    seed_thread_shape(tmp_path, "alpha", [("span1@x", "span0@x")], epoch="1.git")
    alpha = _alpha_live()

    opened = _count_repo_opens(monkeypatch)
    with SessionLocal() as s:
        got = read_messages(s, alpha, ["span0@x", "span1@x"])

    assert set(got) == {"span0@x", "span1@x"}, "a message on one side was dropped"
    assert len(opened) == 2, "one open per epoch, and both epochs are needed"


def test_read_messages_skips_a_missing_epoch_without_losing_the_rest(tmp_path):
    """A mirror gap must degrade to "that message has no body", never
    take out the whole conversation. The route renders each absent key
    as a header row with no body, same as the per-message path's
    `MessageNotFound` branch did."""
    import shutil

    from mimir.extensions import SessionLocal
    from mimir.store import read_messages
    from tests.test_routes._helpers import seed_thread_shape

    seed_thread_shape(tmp_path, "alpha", [("gap0@x", None)], epoch="0.git")
    seed_thread_shape(tmp_path, "alpha", [("gap1@x", "gap0@x")], epoch="1.git")
    shutil.rmtree(tmp_path / "1.git")
    alpha = _alpha_live()

    with SessionLocal() as s:
        got = read_messages(s, alpha, ["gap0@x", "gap1@x"])

    assert set(got) == {"gap0@x"}


def test_read_messages_reads_this_inboxs_pointer_for_a_cross_post(
    tmp_path, monkeypatch
):
    """Inbox scoping is load-bearing, not incidental. A cross-posted
    message has ONE article row and one `article_lists` row per inbox,
    each carrying its own `(epoch, commit_sha)`, because the same
    message is a different commit in each mirror. A join that lost the
    inbox filter reads some other inbox's pointer against THIS inbox's
    mirror.

    Pinned by counting repo opens rather than by comparing bodies: with
    two candidate rows the surviving dict entry depends on iteration
    order, so a body assertion would only fail some of the time.
    Touching a second epoch at all is the defect, and that is
    deterministic.
    """
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList
    from mimir.store import read_messages
    from tests.test_routes._helpers import seed_thread_shape

    seed_thread_shape(tmp_path, "alpha", [("xpost@x", None)], epoch="0.git")
    # A second epoch under the SAME mirror, so a wrong-pointer read
    # actually resolves rather than being saved by the
    # missing-directory guard.
    seed_thread_shape(tmp_path, "alpha", [("elsewhere@x", None)], epoch="1.git")

    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        xpost_id = s.execute(
            sa_select(Article.id).where(Article.message_id == "xpost@x")
        ).scalar_one()
        other_sha = s.execute(
            sa_select(ArticleList.commit_sha)
            .join(Article, Article.id == ArticleList.article_id)
            .where(Article.message_id == "elsewhere@x")
        ).scalar_one()
        # beta's pointer for the same message: different epoch,
        # different commit, exactly as a real cross-post is.
        s.add(
            ArticleList(
                article_id=xpost_id,
                inbox_id=beta.id,
                epoch="1.git",
                commit_sha=other_sha,
            )
        )
        s.commit()

    alpha = _alpha_live()
    opened = _count_repo_opens(monkeypatch)
    with SessionLocal() as s:
        got = read_messages(s, alpha, ["xpost@x"])

    assert len(opened) == 1, f"read another inbox's epoch too: {opened}"
    assert set(got) == {"xpost@x"}
    assert got["xpost@x"].message_id == "xpost@x", "resolved the wrong blob"


def test_read_messages_ignores_a_message_absent_from_this_inbox(tmp_path):
    """art2 is beta-only in the seed, so asking alpha for it yields no
    key: the bulk analogue of `read_message`'s MessageNotFound."""
    from mimir.extensions import SessionLocal
    from mimir.store import read_messages
    from tests.test_routes._helpers import seed_thread_shape

    seed_thread_shape(tmp_path, "alpha", [("mine@x", None)])
    alpha = _alpha_live()

    with SessionLocal() as s:
        got = read_messages(s, alpha, ["mine@x", "art2@example.com"])

    assert set(got) == {"mine@x"}


def test_read_messages_on_an_empty_list_touches_neither_db_nor_disk(monkeypatch):
    from mimir.extensions import SessionLocal
    from mimir.store import read_messages

    opened = _count_repo_opens(monkeypatch)
    with SessionLocal() as s:
        assert read_messages(s, _alpha_live(), []) == {}
    assert opened == []


# web._safe_from_filter, privacy redaction


def test_safe_from_kernel_org_address_surfaces_in_full():
    out = _safe_from_filter("Linus Torvalds <torvalds@linux-foundation.org>")
    # Default email_allowlist contains "torvalds@", full surfaces.
    assert "torvalds@linux-foundation.org" in out


def test_safe_from_kernel_org_domain_surfaces_in_full():
    """`@kernel.org` is in the default allowlist."""
    out = _safe_from_filter("Greg KH <greg@kernel.org>")
    assert "greg@kernel.org" in out


def test_safe_from_other_domain_redacts_address_keeps_name():
    out = _safe_from_filter("Joe User <joe@example.com>")
    assert "joe@example.com" not in out
    assert "Joe User" in out
    assert "<hidden>" in out


def test_safe_from_no_display_name_only_redacts_to_hidden():
    out = _safe_from_filter("anon@example.com")
    assert "anon@example.com" not in out
    assert out == "<hidden>"


def test_safe_from_empty_returns_empty_string():
    assert _safe_from_filter(None) == ""
    assert _safe_from_filter("") == ""


# web._content_disposition, RFC 6266


def test_content_disposition_no_filename():
    assert _content_disposition(None) == "attachment"
    assert _content_disposition("") == "attachment"


def test_content_disposition_ascii_filename():
    cd = _content_disposition("patch.diff")
    # ASCII filename; both forms emitted.
    assert 'filename="patch.diff"' in cd
    assert "filename*=UTF-8''patch.diff" in cd


def test_content_disposition_strips_quote_and_backslash_in_ascii_form():
    """`"` and `\\` in the ASCII filename break the simple
    `filename="..."` quoting; the helper strips them. The full
    name is preserved in the `filename*=` form via percent-encoding."""
    cd = _content_disposition('weird "name" with \\backslash.txt')
    # ASCII form has the quotes and backslashes stripped.
    assert "weird name with backslash.txt" in cd
    # Percent-encoded form preserves them via URL escaping.
    assert "filename*=UTF-8''" in cd
    assert "%22" in cd  # encoded "
    assert "%5C" in cd  # encoded \\


def test_content_disposition_non_ascii_filename():
    """Non-ASCII filenames go through RFC 6266's filename* with
    percent-encoded UTF-8 octets."""
    cd = _content_disposition("café.txt")
    # The filename* form must percent-encode the non-ASCII bytes.
    assert "filename*=UTF-8''" in cd
    # 'é' is U+00E9 → UTF-8 0xC3 0xA9 → %C3%A9
    assert "%C3%A9" in cd


def test_content_disposition_strips_control_bytes_from_ascii_form():
    """Control bytes (CR, LF, NUL, tab, DEL) in the ASCII `filename="…"`
    form would let a maliciously-crafted attachment filename inject
    extra HTTP response headers (RFC 7230 header-line splitting).
    Defense in depth on top of whatever the WSGI layer rejects: strip
    them before they reach the header value. The percent-encoded
    `filename*` form is unaffected, quote() already escapes them."""
    cd = _content_disposition("evil\r\nX-Injected: yes.txt")
    assert "\r" not in cd
    assert "\n" not in cd
    # The ASCII form survives with the control bytes excised.
    assert 'filename="evilX-Injected: yes.txt"' in cd
    # The filename* form percent-encodes them.
    assert "%0D" in cd
    assert "%0A" in cd


# web._redact_trailer_address, DCO trailer redaction


def test_redact_trailer_address_allowlisted_returns_visible_angle_form():
    """Allowlisted addresses surface inside literal angle brackets so the
    rendered output reads `<addr@kernel.org>`, the renderer escapes the
    return value, so the redactor returns plain text with raw angle
    brackets and the browser sees them as visible characters."""
    out = _redact_trailer_address("torvalds@kernel.org")
    assert out == "<torvalds@kernel.org>"


def test_redact_trailer_address_non_allowlisted_returns_redacted():
    """Non-allowlisted addresses collapse to a single `<redacted>` token
    , plain text, again rendered as visible characters after the
     template's escaping pass."""
    out = _redact_trailer_address("random@example.com")
    assert out == "<redacted>"


def test_canonical_inbox_names_falls_back_to_alphabetical_when_null(seeded_db):
    """`_canonical_inbox_names_for` resolves articles to their canonical
    inbox name; when `canonical_inbox_id` is NULL (the warm-up window
    before `admin canonicals backfill` has pinned one), it must fall
    back to the alphabetically-first inbox the article is linked to.

    The seeded conftest leaves all four articles with NULL canonical
    pins. art1 is alpha-only -> 'alpha'; art2 is beta-only -> 'beta';
    art3 is cross-posted to alpha + beta -> 'alpha' (alphabetical
    fallback)."""
    from mimir.models import Article

    with seeded_db() as s:
        ids = {
            row.message_id: row.id for row in s.execute(select(Article)).scalars().all()
        }
        names = _canonical_inbox_names_for(s, list(ids.values()))

    assert names[ids["art1@example.com"]] == "alpha"
    assert names[ids["art2@example.com"]] == "beta"
    assert names[ids["art3@example.com"]] == "alpha"


def test_canonical_inbox_names_prefers_pinned_canonical_over_fallback(seeded_db):
    """If `canonical_inbox_id` is pinned, that beats the alphabetical
    fallback. Pin art3's canonical to beta and confirm the resolver
    returns 'beta' (not the alphabetically-first 'alpha')."""
    from sqlalchemy import update
    from mimir.models import Article, Inbox

    with seeded_db() as s:
        beta_id = s.execute(select(Inbox.id).where(Inbox.name == "beta")).scalar_one()
        art3_id = s.execute(
            select(Article.id).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        s.execute(
            update(Article)
            .where(Article.id == art3_id)
            .values(canonical_inbox_id=beta_id)
        )
        s.commit()
        names = _canonical_inbox_names_for(s, [art3_id])

    assert names[art3_id] == "beta"


def test_canonical_inbox_names_empty_input_returns_empty(seeded_db):
    """Defensive: an empty article_ids list returns {} without
    issuing a query (cli.py:1367-1368 fast-exit)."""
    with seeded_db() as s:
        assert _canonical_inbox_names_for(s, []) == {}


def test_redact_trailer_address_substring_match_is_intentionally_loose(monkeypatch):
    """The allowlist uses substring matching, by design (see CONTEXT.md
    , an allowlist token matches any address containing that substring).
     This is intentional looseness for ergonomics; pin it here so a
     future tightening is a conscious decision, not a silent drift."""
    from mimir.config import settings

    # Set an explicit token to make the assertion deterministic, the
    # default allowlist contains the same token but pinning it here
    # keeps the test independent of the default's evolution.
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    out = _redact_trailer_address("attacker@kernel.org.evil.example")
    # Substring `@kernel.org` matches at position 8 in the address,
    # even though the actual host is `kernel.org.evil.example`. The
    # redactor returns the allowlisted form.
    assert out == "<attacker@kernel.org.evil.example>"


# web.filters._patch_synthesis_filter (SEO W3b synthesis prose)


def _patch_state(*, is_patch=True, trailers=(), landings=(), series=()):
    """Build a PatchState with only the fields the synthesis line
    reads. Constructed directly rather than via `patch_state_for_article`
    so each case pins one branch without seeding a corpus."""
    from mimir.patch_state import PatchState

    return PatchState(
        is_patch=is_patch,
        trailers=list(trailers),
        mainline_landings=list(landings),
        series=list(series),
        days_since_last_reply=None,
    )


def _rev(version, *, current):
    """A revision entry. Only `version` / `is_current` feed the
    synthesis line; the rest is inert filler."""
    from mimir.patch_state import StateSeriesEntry

    return StateSeriesEntry(
        version=version,
        article_id=0,
        date=None,
        url="",
        is_current=current,
        diff_url=None,
    )


def _landing(tree, sha, when=None):
    """A mainline landing. `tree_label` mirrors `tree_name` here; the
    real resolver prettifies it, which the synthesis doesn't depend on."""
    from mimir.patch_state import StateMainlineLanding

    return StateMainlineLanding(
        commit_sha=sha, tree_name=tree, tree_label=tree, committed_at=when
    )


def test_patch_synthesis_composes_revision_review_and_landing_clauses():
    """The full sentence: revision position, review-trailer roll-up
    with the maintainer subset, and the mainline landing. This is the
    indexable restatement of the badges, so the facts have to match
    what the pills claim."""
    from datetime import datetime, timezone

    from mimir.patch_state import StateTrailerCount
    from mimir.web.filters import _patch_synthesis_filter

    state = _patch_state(
        series=[_rev("v1", current=False), _rev("v2", current=True)],
        trailers=[
            StateTrailerCount(role="Reviewed-by", total=3, maintainer_count=2),
            StateTrailerCount(role="Acked-by", total=1, maintainer_count=0),
            # Authorship, not review: must not inflate the count.
            StateTrailerCount(role="Signed-off-by", total=5, maintainer_count=5),
        ],
        landings=[
            _landing(
                "linus", "abc123def4567890", datetime(2026, 6, 1, tzinfo=timezone.utc)
            )
        ],
    )
    out = _patch_synthesis_filter(state)
    assert out == (
        "Revision v2 of 2 in this series; 4 review trailers "
        "(2 from subsystem maintainers); landed in mainline as "
        "abc123def456 on 2026-06-01."
    )


def test_patch_synthesis_prefers_linus_landing_over_earlier_subsystem_tree():
    """A patch that reached mainline carries SEVERAL landings (subsystem
    tree, then linux-next, then Linus), ordered oldest-first. Reporting
    the first row would say "queued in net-next" directly beneath a
    LANDED badge showing the Linus sha, getting wrong the one fact
    ("did this land?") the sentence exists to answer.

    Mirrors `lifecycle_status`'s tree priority: Linus wins when present.
    """
    from datetime import datetime, timezone

    from mimir.web.filters import _patch_synthesis_filter

    state = _patch_state(
        landings=[
            _landing("net-next", "n" * 16, datetime(2026, 6, 1, tzinfo=timezone.utc)),
            _landing("linus", "1" * 16, datetime(2026, 6, 20, tzinfo=timezone.utc)),
        ],
    )
    assert _patch_synthesis_filter(state) == (
        "Landed in mainline as 111111111111 on 2026-06-20."
    )


def test_patch_synthesis_reports_earliest_tree_as_queued_when_not_in_mainline():
    """With no Linus landing the patch has NOT landed, so the sentence
    says "queued", matching the QUEUED badge, and names the earliest
    non-Linus tree (again mirroring the badge)."""
    from datetime import datetime, timezone

    from mimir.web.filters import _patch_synthesis_filter

    state = _patch_state(
        landings=[
            _landing("net-next", "a" * 16, datetime(2026, 6, 1, tzinfo=timezone.utc)),
            _landing("linux-next", "b" * 16, datetime(2026, 6, 5, tzinfo=timezone.utc)),
        ],
    )
    assert _patch_synthesis_filter(state) == (
        "Queued in net-next as aaaaaaaaaaaa on 2026-06-01."
    )


def test_patch_synthesis_empty_for_non_patch_and_bare_patch():
    """Renders nothing when there's nothing to say, so the template
    can call it unconditionally: a non-patch article, and a patch with
    no revisions / reviews / landing, both yield ""."""
    from mimir.web.filters import _patch_synthesis_filter

    assert _patch_synthesis_filter(None) == ""
    assert _patch_synthesis_filter(_patch_state(is_patch=False)) == ""
    assert _patch_synthesis_filter(_patch_state()) == ""


def test_patch_synthesis_singularises_and_omits_absent_clauses():
    """One review reads "1 review trailer" (not "trailers"), the
    maintainer parenthetical is omitted at zero, and a single-revision
    patch gets no revision clause (matching `_revisions_fold.html`,
    which only renders at >= 2)."""
    from mimir.patch_state import StateTrailerCount
    from mimir.web.filters import _patch_synthesis_filter

    state = _patch_state(
        series=[_rev("v1", current=True)],
        trailers=[StateTrailerCount(role="Tested-by", total=1, maintainer_count=0)],
    )
    assert _patch_synthesis_filter(state) == "1 review trailer."
