"""Pull requests to Linus on `/merge-window` (#703): `[GIT PULL]`
messages addressed to Linus since the anchor tag (the latest tag, or
the one before it while the merge window is open), merged when git has
the tip or pr-tracker-bot says it was merged. Unmerged ones are
rejected (Linus replied he will not pull it), unknown (no commit named)
or pending (#803)."""

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import mimir.linus_pulls as lp
import mimir.merge_window as mw
from mimir import store
from mimir.config import settings
from mimir.linus_pulls import linus_pulls
from mimir.merge_window import MERGE_WINDOW_PATH, merge_window_state
from mimir.models import Article, ArticleList, Inbox, MainlineState
from mimir.parser import ParsedArticle

_TAG = "v7.3-rc7"
_TAG_TIME = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
# Commit time per tag: a week per rc, the release a week after rc7.
_TAG_TIMES = {
    "v7.3-rc6": _TAG_TIME - timedelta(days=7),
    _TAG: _TAG_TIME,
    "v7.3": _TAG_TIME + timedelta(days=7),
}
_LINUS = "Linus Torvalds <torvalds@linux-foundation.org>"
_BOT = "pr-tracker-bot@kernel.org"
_TIP_A = "a" * 40
_TIP_B = "b" * 40
# Before the fixture stubs it, for the tests that run the real git.
_REAL_MERGED_TIPS = lp._merged_tips


@pytest.fixture
def tags():
    """Linus's version tags, lowest first; the rc phase by default."""
    return ["v7.3-rc6", _TAG]


@pytest.fixture
def revlist_tags():
    """The tag each `git rev-list <tag>..HEAD` was asked for."""
    return []


@pytest.fixture
def messages(monkeypatch, tags, revlist_tags):
    """Bodies and headers by message-id, served where `read_message`
    would read them from git. The tags, their commit times and the set
    `git rev-list <tag>..HEAD` returns are stubbed alongside."""
    bodies: dict[str, ParsedArticle] = {}

    def _read(session, inbox, message_id):
        if message_id not in bodies:
            raise store.MessageNotFound(message_id)
        return bodies[message_id]

    monkeypatch.setattr(store, "read_message", _read)
    for module in (lp, mw):
        monkeypatch.setattr(module, "version_tags", lambda path: tags)
    monkeypatch.setattr(lp, "_tag_time", lambda path, tag: _TAG_TIMES[tag])
    merged: set[str] = set()

    def _revlist(path, tag):
        revlist_tags.append(tag)
        return merged

    monkeypatch.setattr(lp, "_merged_tips", _revlist)
    return bodies, merged


def _post(
    s,
    bodies,
    msgid,
    *,
    subject,
    author="Mae Maintainer <mae@example.com>",
    to=_LINUS,
    cc="",
    body="",
    hours=24,
    parent=None,
):
    alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    art = Article(
        message_id=msgid,
        subject=subject,
        author=author,
        date=_TAG_TIME + timedelta(hours=hours),
        thread_parent=parent,
        lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="f" * 40)],
    )
    s.add(art)
    s.flush()
    bodies[msgid] = ParsedArticle(
        message_id=msgid, body=body, headers={"To": to, "Cc": cc}
    )
    return art


def _pull(s, bodies, msgid, tip=None, **kw):
    body = "The following changes since commit 0123:\n\n  Linux 7.3-rc7\n\n"
    if tip:
        body += f"are available in the Git repository at:\n\nfor you to fetch changes up to {tip}:\n\n  fix things\n"
    return _post(s, bodies, msgid, subject=f"[GIT PULL] {msgid}", body=body, **kw)


def _bot_reply(s, bodies, parent, *, hours, author=_BOT, merged=True):
    body = "The pull request you sent on Mon, 12 Oct 2026:\n\n" + (
        "has been merged into torvalds/linux.git:\nhttps://git.kernel.org/torvalds/c/1234\n"
        if merged
        else "is still pending.\n"
    )
    return _post(
        s,
        bodies,
        f"bot-{parent}",
        subject=f"Re: [GIT PULL] {parent}",
        author=author,
        to="Mae Maintainer <mae@example.com>",
        body=body,
        hours=hours,
        parent=parent,
    )


def _walked(s, hours):
    state = s.get(MainlineState, "linus") or MainlineState(tree_name="linus")
    state.last_walked_at = _TAG_TIME + timedelta(hours=hours)
    s.add(state)


def _rows(s):
    return [(r.subject, r.state) for r in linus_pulls(s).rows]


def _linus_reply(s, bodies, parent, text, *, hours=12, author=_LINUS):
    return _post(
        s,
        bodies,
        f"reply-{parent}-{hours}",
        subject=f"Re: [GIT PULL] {parent}",
        author=author,
        to="Mae Maintainer <mae@example.com>",
        body=text,
        hours=hours,
        parent=parent,
    )


def test_merged_when_git_has_the_tip_newest_first(seeded_db, messages):
    """The tip comes from the body's "up to <sha>" line; it is merged
    when `rev-list <tag>..HEAD` has it. A pull sent before the tag is
    not a candidate. A pull whose body names no tip is unknown."""
    bodies, merged = messages
    merged.add(_TIP_A)
    with seeded_db() as s:
        _pull(s, bodies, "old@x", _TIP_A, hours=-1)
        _pull(s, bodies, "a@x", _TIP_A, hours=10)
        _pull(s, bodies, "b@x", _TIP_B, hours=20)
        _pull(s, bodies, "notip@x", hours=30)
        s.commit()
        assert _rows(s) == [
            ("[GIT PULL] notip@x", "unknown"),
            ("[GIT PULL] b@x", "pending"),
            ("[GIT PULL] a@x", "merged"),
        ]


@pytest.mark.parametrize(
    ("to", "cc", "listed"),
    [
        (_LINUS, "", True),
        ("Mae <mae@example.com>", "TORVALDS@Linux-Foundation.org", True),
        ("Greg <gregkh@example.com>", "", False),
        ("Not Linus <nottorvalds@linux-foundation.org>", "", False),
        ("torvalds@linux-foundation.org.example.com", "", False),
    ],
)
def test_only_pulls_addressed_to_linus(seeded_db, messages, to, cc, listed):
    """Linus in To or Cc, by exact address, any case. A pull to a
    subsystem maintainer is not a pull to Linus."""
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "pr@x", _TIP_A, to=to, cc=cc)
        s.commit()
        assert [subject for subject, _ in _rows(s)] == (
            ["[GIT PULL] pr@x"] if listed else []
        )


def test_bot_reply_marks_merged_for_good(seeded_db, messages):
    """pr-tracker-bot replies once the merge is in Linus's tree, so its
    reply counts even after a newer walk whose `rev-list` lacks the
    named tip (a re-pushed tag, or a different tip pulled)."""
    bodies, merged = messages
    with seeded_db() as s:
        _walked(s, 1)
        _pull(s, bodies, "pr@x", _TIP_A, hours=10)
        _bot_reply(s, bodies, "pr@x", hours=12)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "merged")]

        _walked(s, 13)
        s.commit()
        assert _TIP_A not in merged  # precondition: git disagrees
        assert _rows(s) == [("[GIT PULL] pr@x", "merged")]


def test_new_walk_recomputes_the_merged_set(seeded_db, messages):
    """A tip reaching Linus's tree shows once a walk records it: the
    walk timestamp moves the section's cache key. Served through the
    cache both times, not cleared."""
    bodies, merged = messages
    with seeded_db() as s:
        _walked(s, 1)
        _pull(s, bodies, "pr@x", _TIP_A, hours=10)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "pending")]

        merged.add(_TIP_A)
        _walked(s, 13)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "merged")]


@pytest.mark.parametrize(
    ("author", "says_merged"),
    [("Mae <mae@example.com>", True), (_BOT, False)],
)
def test_reply_that_is_not_a_bot_merge_notice_does_not_count(
    seeded_db, messages, author, says_merged
):
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "pr@x", _TIP_A, hours=10)
        _bot_reply(s, bodies, "pr@x", hours=12, author=author, merged=says_merged)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "pending")]


@pytest.mark.parametrize(
    "text",
    [
        "I'm not pulling this.",
        "NOT PULLING, sorry",
        "I am not going to pull this mess.",
        "Sorry, I won\u2019t pull it.",
        "wont pull",
        "Unpulled.",
        "Good morning.\n\nThis is unpulled until you fix it.\n",
        # He wraps at about 72 columns, so a phrase can straddle a break.
        "This is a long line explaining the problem and so I'm not\npulling this.\n",
        "Sorry, but with all those unsigned commits I am not going\nto pull it.\n",
    ],
)
@pytest.mark.parametrize("tip", [_TIP_A, None])
def test_linus_rejection_phrases(seeded_db, messages, text, tip):
    """A tipless pull is rejected too: the rejection is checked before
    unknown."""
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "pr@x", tip, hours=10)
        _linus_reply(s, bodies, "pr@x", text)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "rejected")]


@pytest.mark.parametrize(
    ("author", "text"),
    [
        (_LINUS, "> I am not pulling this\n  > unpulled\nLooks fine.\n"),
        ("Mae <mae@example.com>", "I'm not pulling this."),
        (_BOT, "I'm not pulling this."),
        (_LINUS, "I refuse. NAK. Pulling later, perhaps.\n"),
        (_LINUS, "Pulled, thanks. Nothing unpulling here.\n"),
    ],
)
def test_reply_that_is_not_a_linus_rejection_does_not_count(
    seeded_db, messages, author, text
):
    """Quoted lines are the maintainer's words, a reply from anyone but
    Linus is not his decision, and the phrase list is narrow."""
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "pr@x", _TIP_A, hours=10)
        _linus_reply(s, bodies, "pr@x", text, author=author)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "pending")]


def test_tipless_pull_with_a_bot_merge_reply_is_merged(seeded_db, messages):
    """Unknown means no commit named AND no bot reply: the bot's word is
    enough without a tip, and it wins over a rejection."""
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "pr@x", hours=10)
        _linus_reply(s, bodies, "pr@x", "Not pulling this.", hours=11)
        _bot_reply(s, bodies, "pr@x", hours=12)
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "merged")]


def test_merged_wins_over_a_rejection_reply(seeded_db, messages):
    bodies, merged = messages
    merged.add(_TIP_A)
    with seeded_db() as s:
        _pull(s, bodies, "pr@x", _TIP_A, hours=10)
        _linus_reply(s, bodies, "pr@x", "Not pulling this one.")
        s.commit()
        assert _rows(s) == [("[GIT PULL] pr@x", "merged")]


def test_linus_reply_moves_the_state_and_the_rows(seeded_db, messages):
    """A reply titled "Re: [GIT PULL] ..." is not a `[GIT PULL]%`
    subject, so Linus's address is what moves the state. Served through
    the cache both times, not cleared."""
    from mimir.linus_pulls import linus_pulls_state

    bodies, _ = messages
    with seeded_db() as s:
        _walked(s, 1)
        _pull(s, bodies, "pr@x", _TIP_A, hours=10)
        s.commit()
        before = linus_pulls_state(s)
        assert _rows(s) == [("[GIT PULL] pr@x", "pending")]

        _linus_reply(s, bodies, "pr@x", "Not pulling this.")
        s.commit()
        assert linus_pulls_state(s) != before
        assert _rows(s) == [("[GIT PULL] pr@x", "rejected")]


def test_failing_git_shows_pending_logged_once(
    seeded_db, messages, monkeypatch, tmp_path, caplog
):
    """A mirror git cannot read (missing, or git failing) shows every
    pull as pending, with one warning per compute rather than one per
    pull."""
    from mimir.config import TreeConfig

    monkeypatch.setattr(lp, "_merged_tips", _REAL_MERGED_TIPS)
    monkeypatch.setitem(
        settings.trees,
        "linus",
        TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
            path=tmp_path / "absent.git",
        ),
    )
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "a@x", _TIP_A, hours=10)
        _pull(s, bodies, "b@x", _TIP_B, hours=20)
        s.commit()
        with caplog.at_level(logging.WARNING, logger="mimir.linus_pulls"):
            assert _rows(s) == [
                ("[GIT PULL] b@x", "pending"),
                ("[GIT PULL] a@x", "pending"),
            ]
    assert len([r for r in caplog.records if r.name == "mimir.linus_pulls"]) == 1


def test_stalled_git_shows_pending(seeded_db, messages, monkeypatch, caplog):
    """A `rev-list` that outlives its timeout shows pending instead of
    holding a web worker; the timeout is actually passed to git."""
    import subprocess

    monkeypatch.setattr(lp, "_merged_tips", _REAL_MERGED_TIPS)
    timeouts = []

    def _stall(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(lp.subprocess, "run", _stall)
    bodies, _ = messages
    with seeded_db() as s:
        _pull(s, bodies, "a@x", _TIP_A, hours=10)
        s.commit()
        with caplog.at_level(logging.WARNING, logger="mimir.linus_pulls"):
            assert _rows(s) == [("[GIT PULL] a@x", "pending")]
    assert timeouts == [lp.GIT_REV_LIST_TIMEOUT_SEC]
    assert len([r for r in caplog.records if r.name == "mimir.linus_pulls"]) == 1


def _linus_repo(tmp_path):
    """A bare repo: an annotated `v7.3-rc7` on the first commit, HEAD
    one commit later."""
    from dulwich.objects import Commit, Tag, Tree
    from dulwich.repo import Repo

    repo = Repo.init_bare(str(tmp_path / "linux.git"), mkdir=True)
    tree = Tree()
    repo.object_store.add_object(tree)
    commits = []
    for n, when in enumerate((1760212800, 1760299200)):
        c = Commit()
        c.tree = tree.id
        c.parents = [commits[-1].id] if commits else []
        c.author = c.committer = b"L <l@x>"
        c.author_time = c.commit_time = when
        c.author_timezone = c.commit_timezone = -7 * 3600
        c.message = f"c{n}".encode()
        repo.object_store.add_object(c)
        commits.append(c)
    tag = Tag()
    tag.name = _TAG.encode()
    tag.object = (Commit, commits[0].id)
    tag.tagger = b"L <l@x>"
    tag.tag_time = 1760212900
    tag.tag_timezone = 0
    tag.message = b"Linux 7.3-rc7"
    repo.object_store.add_object(tag)
    repo.refs[b"refs/tags/" + tag.name] = tag.id
    repo.refs[b"refs/heads/master"] = commits[1].id
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/master")
    return tmp_path / "linux.git", commits


def test_tag_time_and_merged_tips_read_the_real_mirror(tmp_path):
    """The annotated tag peels to its commit, whose commit time (in
    UTC) bounds the candidates; `rev-list <tag>..HEAD` is everything
    after it, and not the tagged commit itself."""
    path, commits = _linus_repo(tmp_path)
    assert lp._tag_time(path, _TAG) == datetime.fromtimestamp(1760212800, UTC)
    assert lp._merged_tips(path, _TAG) == {commits[1].id.decode()}
    assert lp._tag_time(tmp_path / "absent.git", _TAG) is None


def _pulls_heading_before_queue(html):
    pulls = html.index('id="pull-requests-to-linus"')
    queue = html.index("Nothing is queued")
    return pulls < queue


@pytest.mark.parametrize(
    ("latest", "first"),
    [(["v7.3-rc7", "v7.3"], True), (["v7.4-rc2", "v7.4-rc3"], False)],
)
def test_pulls_come_first_while_the_window_is_open(
    client, messages, monkeypatch, latest, first
):
    for module in (lp, mw):
        monkeypatch.setattr(module, "version_tags", lambda path: latest)
    monkeypatch.setattr(lp, "_tag_time", lambda path, tag: _TAG_TIME)
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    assert f"No pull requests to Linus since {latest[-2 if first else -1]}." in html
    assert _pulls_heading_before_queue(html) is first


def test_new_pull_moves_the_etag_not_the_queue_key(client, messages):
    """New mail moves the page's validator through the section's own
    state, and leaves the linux-next queue's state (its cache key)
    alone. Caches are primed by an ordinary request, not cleared, so a
    cache the validator cannot see would show here."""
    from mimir.extensions import SessionLocal

    bodies, merged = messages
    merged.add(_TIP_A)
    with SessionLocal() as s:
        _pull(s, bodies, "first@x", _TIP_A)
        s.commit()
    first = client.get(MERGE_WINDOW_PATH)
    assert "[GIT PULL] first@x" in first.get_data(as_text=True)  # primed
    with SessionLocal() as s:
        queue_state = merge_window_state(s)
        art = _pull(s, bodies, "second@x", _TIP_B, hours=30)
        s.commit()
        url = f"/alpha/{art.date:%Y}/{art.date:%m}/{art.id}"
        assert merge_window_state(s) == queue_state
    again = client.get(
        MERGE_WINDOW_PATH, headers={"If-None-Match": first.headers["ETag"]}
    )
    assert again.status_code == 200
    assert again.headers["ETag"] != first.headers["ETag"]
    html = again.get_data(as_text=True)
    assert _columns(html) == {"pending": 1, "merged": 1}
    assert f'<a href="{url}">[GIT PULL] second@x</a>' in _column_html(html, "pending")
    assert "[GIT PULL] first@x</a>" in _column_html(html, "merged")


def test_linus_mirror_path_moves_the_section_key(seeded_db, monkeypatch, tmp_path):
    """The mirror path is restart-only config the section reads git
    from, so it is in the section's own state: the page ETag moving
    through the queue's state alone would leave this cache stale. (The
    anchor tag is not: tags move only in a walk, and the walk
    timestamp is in the state.)"""
    from mimir.config import TreeConfig
    from mimir.linus_pulls import linus_pulls_state

    with seeded_db() as s:
        before = linus_pulls_state(s)
        monkeypatch.setitem(
            settings.trees,
            "linus",
            TreeConfig(
                url="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
                path=tmp_path / "elsewhere.git",
            ),
        )
        assert linus_pulls_state(s) != before


def test_demoted_inboxes_move_the_section_key(seeded_db, monkeypatch):
    """The demoted list decides which inbox each pull links into, and it
    is restart-only config, so it is in the section's own state for the
    same reason as the mirror path."""
    from mimir.linus_pulls import linus_pulls_state

    with seeded_db() as s:
        before = linus_pulls_state(s)
        monkeypatch.setattr(
            settings,
            "canonical_demoted_inboxes",
            [*settings.canonical_demoted_inboxes, "alpha"],
        )
        assert linus_pulls_state(s) != before


@pytest.mark.parametrize(
    ("version_tags", "anchor"),
    [
        (["v7.3-rc6", "v7.3-rc7"], "v7.3-rc7"),
        (["v7.2", "v7.3-rc7", "v7.3"], "v7.3-rc7"),
        (["v7.3"], "v7.3"),
        ([], None),
    ],
)
def test_anchor_is_the_tag_before_an_open_window(monkeypatch, version_tags, anchor):
    """In the rc phase, the latest tag. While the window is open (the
    latest tag is a release), the tag before it. A mirror with only the
    release falls back to it."""
    monkeypatch.setattr(lp, "version_tags", lambda path: version_tags)
    assert lp.anchor_tag(None) == anchor


def test_open_window_counts_rc_week_pulls_against_the_last_rc(
    seeded_db, messages, tags, revlist_tags, monkeypatch
):
    """Early merge-window pulls are sent in the last rc week, before the
    release is tagged, and an rc-week fixes pull can be merged before
    the release. Both count from the last rc, for the mails and for
    `rev-list` alike: from the release, the first is not a candidate
    and the second is not after the tag."""
    tags.append("v7.3")
    bodies, _ = messages
    after = {_TAG: {_TIP_A, _TIP_B}, "v7.3": {_TIP_B}}

    def _revlist(path, tag):
        revlist_tags.append(tag)
        return after[tag]

    monkeypatch.setattr(lp, "_merged_tips", _revlist)
    with seeded_db() as s:
        _pull(s, bodies, "fixes@x", _TIP_A, hours=24)
        _pull(s, bodies, "early@x", _TIP_B, hours=48)
        _pull(s, bodies, "rc6week@x", _TIP_B, hours=-24)
        s.commit()
        assert _rows(s) == [
            ("[GIT PULL] early@x", "merged"),
            ("[GIT PULL] fixes@x", "merged"),
        ]
        assert linus_pulls(s).anchor == _TAG
    assert revlist_tags == [_TAG]


def _column_html(html, state):
    return re.search(
        rf'<section class="pull-column" id="pulls-{state}">(.*?)</section>',
        html,
        re.DOTALL,
    ).group(1)


def _columns(html):
    """Heading count per rendered column, in page order."""
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(
            r'<section class="pull-column" id="pulls-(\w+)">\s*'
            r"<h3>[^<]*<small>(\d+)</small>",
            html,
        )
    }


def test_columns_render_only_when_populated(client, messages):
    from mimir.extensions import SessionLocal

    bodies, merged = messages
    merged.add(_TIP_A)
    with SessionLocal() as s:
        _pull(s, bodies, "a@x", _TIP_A, hours=10)
        _pull(s, bodies, "b@x", _TIP_B, hours=20)
        s.commit()
        empty = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
        _pull(s, bodies, "u@x", hours=30)
        _pull(s, bodies, "r@x", _TIP_B, hours=40)
        _linus_reply(s, bodies, "r@x", "Not pulling this.")
        s.commit()
    full = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    assert "Names no commit" not in empty
    assert "Names no commit" in _column_html(full, "unknown")
    assert list(_columns(empty).items()) == [("pending", 1), ("merged", 1)]
    assert list(_columns(full).items()) == [
        ("pending", 1),
        ("merged", 1),
        ("unknown", 1),
        ("rejected", 1),
    ]
