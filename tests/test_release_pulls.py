"""Pull requests to Linus per closed release on its history page (#1135):
classified once by the hourly warm with `/merge-window`'s classifier and
kept under the release's immutable key; a page view never reads a mail."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from dulwich.repo import Repo

from mimir import cache, store
from mimir import release_history as history
from mimir.parser import ParsedArticle
from tests._dom import parse
from tests._helpers import add_commit
from tests.test_linus_pulls import (
    _BOT,
    _TAG_TIME,
    _TIP_B,
    _bot_reply,
    _linus_reply,
    _post,
    _pull,
)

# v7.2 is tagged at the linus_pulls helpers' `_TAG_TIME`, so their
# `hours=` offsets count from vPREV. Nine weeks a cycle.
_CYCLE = timedelta(days=63)
_HOURS_TO_V73 = int(_CYCLE.total_seconds() // 3600)


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """Lightweight tags v7.1 to v7.4, nine weeks apart, with one commit
    inside each range: `old` lands in v7.2, `new` in v7.3, `next` in
    v7.4."""
    path = tmp_path / "linus.git"
    repo = Repo.init_bare(path, mkdir=True)
    author = b"Linus <linus@x>"
    t72 = int(_TAG_TIME.timestamp())
    t71, t73 = t72 - int(_CYCLE.total_seconds()), t72 + int(_CYCLE.total_seconds())
    v71 = add_commit(repo, author, [], b"7.1", when=t71).id
    old = add_commit(repo, author, [v71], b"old", when=t71 + 3600).id
    v72 = add_commit(repo, author, [old], b"7.2", when=t72).id
    new = add_commit(repo, author, [v72], b"new", when=t72 + 3600).id
    v73 = add_commit(repo, author, [new], b"7.3", when=t73).id
    nxt = add_commit(repo, author, [v73], b"next", when=t73 + 3600).id
    t74 = t73 + int(_CYCLE.total_seconds())
    v74 = add_commit(repo, author, [nxt], b"7.4", when=t74).id
    for tag, oid in (("v7.1", v71), ("v7.2", v72), ("v7.3", v73), ("v7.4", v74)):
        repo.refs[b"refs/tags/" + tag.encode()] = oid
    repo.close()
    monkeypatch.setattr(history, "_linus_path", lambda: path)
    # The tags are dated up to 2027; the fill waits until a tag has settled
    # (PULLS_SETTLE), on the cache's clock, so set that clock past them.
    settled = t74 + 30 * 86400
    monkeypatch.setattr(cache, "_now", lambda: settled)
    return {"old": old.decode(), "new": new.decode(), "next": nxt.decode()}


@pytest.fixture
def bodies(monkeypatch):
    """Mail bodies by message-id, served where `read_messages` reads git.
    `reads` counts every batch read, so a test can assert none happened."""
    held: dict[str, ParsedArticle] = {}
    reads = []

    def read_many(session, inbox, message_ids):
        reads.append(list(message_ids))
        return {m: held[m] for m in message_ids if m in held}

    def read_one(*args):
        pytest.fail("pull bodies are read per batch, never per mail")

    monkeypatch.setattr(store, "read_messages", read_many)
    monkeypatch.setattr(store, "read_message", read_one)
    return held, reads


def _release(tag):
    (release,) = [r for r in history.release_catalog() if r.tag == tag]
    return release


def _v73():
    return _release("v7.3")


def _fill(seeded_db, *releases):
    """What the hourly warm does: the Git facts of each release and its
    predecessor (as `warm_release` leaves them), then the pull fill over
    the whole catalog, which classifies whatever has both."""
    catalog = history.release_catalog()
    by_tag = {r.tag: r for r in catalog}
    for release in releases:
        history.git_facts(release)
        if release.previous_tag:
            history.git_facts(by_tag[release.previous_tag])
    with seeded_db() as s:
        history.warm_release_pulls(s, catalog)


def _states(release):
    rows = cache.get(release.key + history.PULL_ROWS)
    return {_mid(article_id): state for article_id, state in rows}


def _mid(article_id):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        return s.get(Article, article_id).message_id


def test_window_rule_for_a_closed_release(seeded_db, mirror, bodies):
    """Candidates run from 14 days before vPREV to vN. A pull sent in
    vPREV's last two weeks counts for vPREV only if it made it into vPREV
    or Linus rejected it; otherwise it counts here, with this release's
    state. A pr-tracker-bot reply marks merged only inside vPREV..vN."""
    held, _ = bodies
    day = 24
    with seeded_db() as s:
        # Lead-in: sent before v7.2, inside the 14 days.
        _pull(s, held, "early-for-v73@x", mirror["new"], hours=-3 * day)
        _pull(s, held, "merged-in-v72@x", mirror["old"], hours=-3 * day)
        _pull(s, held, "lead-in-unclear@x", hours=-3 * day)
        _pull(s, held, "lead-in-unmerged@x", _TIP_B, hours=-3 * day)
        # Before the lead-in, and after the release: not candidates.
        _pull(s, held, "too-early@x", mirror["new"], hours=-15 * day)
        _pull(s, held, "after-v73@x", mirror["new"], hours=_HOURS_TO_V73 + 1)
        # The cycle itself.
        _pull(s, held, "merged@x", mirror["new"], hours=2 * day)
        _pull(s, held, "not-merged@x", _TIP_B, hours=2 * day)
        _pull(s, held, "unclear@x", hours=2 * day)
        _pull(s, held, "rejected@x", _TIP_B, hours=2 * day)
        _linus_reply(s, held, "rejected@x", "Not pulling this.", hours=3 * day)
        _pull(s, held, "bot@x", hours=2 * day)
        _bot_reply(s, held, "bot@x", hours=3 * day)
        _pull(s, held, "bot-late@x", hours=2 * day)
        _bot_reply(s, held, "bot-late@x", hours=_HOURS_TO_V73 + 2)
        s.commit()
    release = _v73()
    _fill(seeded_db, release)
    assert _states(release) == {
        "early-for-v73@x": "merged",
        "lead-in-unclear@x": "unknown",
        "lead-in-unmerged@x": "not_merged",
        "merged@x": "merged",
        "not-merged@x": "not_merged",
        "unclear@x": "unknown",
        "rejected@x": "rejected",
        "bot@x": "merged",
        "bot-late@x": "unknown",
    }
    summary = cache.get(release.key + history.PULLS)
    assert summary == {"count": 9, "unclear": 3, "tips": 5, "bot_replies": 1}


# The first hour of v7.4's lead-in: exactly 14 days before v7.3's tag.
_LATE = _HOURS_TO_V73 - 14 * 24


def test_late_pulls_count_for_the_next_release_unless_they_made_it(
    seeded_db, mirror, bodies
):
    """A pull sent in the last 14 days before vN that did not make it into
    vN, and that Linus did not reject, counts for vN+1 with vN+1's state:
    merged, not merged or Unclear. It is on exactly one page."""
    held, _ = bodies
    with seeded_db() as s:
        # On the boundary itself, and merged into v7.4.
        _pull(s, held, "late-for-v74@x", mirror["next"], hours=_LATE)
        # One hour before it: still v7.3's, so not merged in v7.3.
        _pull(s, held, "last-in-cycle@x", _TIP_B, hours=_LATE - 1)
        _pull(s, held, "late-merged@x", mirror["new"], hours=_LATE + 24)
        _pull(s, held, "late-rejected@x", _TIP_B, hours=_LATE + 24)
        _linus_reply(s, held, "late-rejected@x", "Not pulling this.", hours=_LATE + 48)
        _pull(s, held, "late-unclear@x", hours=_LATE + 24)
        _pull(s, held, "late-nowhere@x", _TIP_B, hours=_LATE + 24)
        s.commit()
    v73, v74 = _release("v7.3"), _release("v7.4")
    _fill(seeded_db, v73, v74)
    assert _states(v73) == {
        "late-merged@x": "merged",
        "late-rejected@x": "rejected",
        "last-in-cycle@x": "not_merged",
    }
    assert _states(v74) == {
        "late-for-v74@x": "merged",
        "late-unclear@x": "unknown",
        "late-nowhere@x": "not_merged",
    }


def test_a_pull_sent_at_the_tag_instant_is_in_the_overlap(seeded_db, mirror, bodies):
    """The overlap is closed at both ends: a pull sent exactly at v7.3's
    tag is v7.3's if it made it in, else v7.4's, never both."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "at-tag-in@x", mirror["new"], hours=_HOURS_TO_V73)
        _pull(s, held, "at-tag-out@x", mirror["next"], hours=_HOURS_TO_V73)
        s.commit()
    v73, v74 = _release("v7.3"), _release("v7.4")
    _fill(seeded_db, v73, v74)
    assert _states(v73) == {"at-tag-in@x": "merged"}
    assert _states(v74) == {"at-tag-out@x": "merged"}


def test_a_pull_merged_in_vprev_with_a_late_bot_reply_is_not_merged_in_vn(
    seeded_db, mirror, bodies
):
    """#1135 acceptance: "one merged in vPREV does not appear on vN".
    Release-day fix pulls are merged, then tagged, then pushed; the bot
    sees the push and replies after the tag. The tip says vPREV.
    (Review finding 2, kept as written.)"""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "final-fixes@x", mirror["old"], hours=-6)
        _bot_reply(s, held, "final-fixes@x", hours=1)
        s.commit()
    release = _v73()
    _fill(seeded_db, release)
    assert "final-fixes@x" not in _states(release)


def test_a_tipless_lead_in_pull_goes_by_its_bot_reply_date(seeded_db, mirror, bodies):
    """No tip: a pr-tracker-bot reply at or before vPREV's tag says it made
    it into vPREV; one after it says vN merged it. With two replies, the
    first decides."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "in-v72@x", hours=-48)
        _bot_reply(s, held, "in-v72@x", hours=0)
        _post(
            s,
            held,
            "bot-again@x",
            subject="Re: [GIT PULL] in-v72@x",
            author=_BOT,
            body="has been merged into torvalds/linux.git:\n",
            hours=5,
            parent="in-v72@x",
        )
        _pull(s, held, "in-v73@x", hours=-48)
        _bot_reply(s, held, "in-v73@x", hours=1)
        s.commit()
    v72, v73 = _release("v7.2"), _release("v7.3")
    _fill(seeded_db, v72, v73)
    assert _states(v72) == {"in-v72@x": "merged"}
    assert _states(v73) == {"in-v73@x": "merged"}


def test_every_candidate_pull_is_on_exactly_one_page(seeded_db, mirror, bodies):
    """v7.2, v7.3 and v7.4 side by side, with every kind of pull at the
    edges and inside both overlaps and in each middle: each is on exactly
    one page. v7.4's own tail is left out: what did not make it into v7.4
    belongs to v7.5, which has no page here (the late-pull test covers
    that tail)."""
    held, _ = bodies
    t73 = _HOURS_TO_V73
    offsets = [-14 * 24, -72, 0, 120, t73 - 14 * 24, t73 - 24, t73, t73 + 120]
    # "refused-next" is refused, then merged into v7.4 all the same.
    kinds = ["old", "new", "next", "nowhere", "unclear", "bot", "refused"]
    kinds += ["refused-next"]
    mids = []
    with seeded_db() as s:
        for hours in offsets:
            for kind in kinds:
                mid = f"{kind}-{hours}@x"
                tip = {
                    "nowhere": _TIP_B,
                    "refused": _TIP_B,
                    "refused-next": mirror["next"],
                }.get(kind, mirror.get(kind))
                _pull(s, held, mid, tip, hours=hours)
                if kind == "bot":
                    _bot_reply(s, held, mid, hours=hours + 1)
                if kind.startswith("refused"):
                    _linus_reply(s, held, mid, "Not pulling this.", hours=hours + 2)
                mids.append(mid)
        s.commit()
    pages = [_release(t) for t in ("v7.2", "v7.3", "v7.4")]
    _fill(seeded_db, *pages)
    seen = [mid for release in pages for mid in _states(release)]
    # Precondition: every page classified something.
    assert all(_states(release) for release in pages)
    assert sorted(seen) == sorted(mids)


def test_rows_are_newest_first(seeded_db, mirror, bodies):
    held, _ = bodies
    with seeded_db() as s:
        for n, hours in enumerate((30, 10, 20)):
            _pull(s, held, f"p{n}@x", hours=hours)
        s.commit()
    release = _v73()
    _fill(seeded_db, release)
    assert list(_states(release)) == ["p0@x", "p2@x", "p1@x"]


def _page(client, tag="v7.3", page=None):
    path = history.release_history_path(tag) + (f"?page={page}" if page else "")
    response = client.get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _cards(html):
    return {
        c.attrs["id"].removeprefix("pulls-"): (
            c.find("h3").text(),
            int(c.find("p", cls="count").text()),
        )
        for c in parse(html).find_all("section", cls="pull-card")
    }


def _seed_four_states(seeded_db, mirror, held):
    with seeded_db() as s:
        _pull(s, held, "merged@x", mirror["new"], hours=48)
        _pull(s, held, "not-merged@x", _TIP_B, hours=48)
        _pull(s, held, "unclear@x", hours=48)
        _pull(s, held, "rejected@x", _TIP_B, hours=48)
        _linus_reply(s, held, "rejected@x", "Not pulling this.", hours=72)
        s.commit()


def test_detail_shows_the_four_closed_states_without_reading_mail(
    client, seeded_db, mirror, bodies
):
    held, reads = bodies
    _seed_four_states(seeded_db, mirror, held)
    _fill(seeded_db, _v73())
    assert reads, "precondition: the fill read the bodies"
    reads.clear()
    html = _page(client)
    assert reads == []
    assert _cards(html) == {
        "merged": ("Merged", 1),
        "not_merged": ("Not merged in v7.3", 1),
        "unknown": ("Unclear", 1),
        "rejected": ("Rejected", 1),
    }
    assert "Waiting" not in html
    # No subsystem grouping is cached, so the message list's heading is
    # there because of the pulls alone.
    assert 'id="subsystems"' not in html
    assert [h.text() for h in parse(html).find_all("h2")] == [
        "Pull requests to Linus",
        "Archived messages",
    ]


def test_live_and_history_take_their_labels_from_one_include(
    client, seeded_db, mirror, bodies, monkeypatch
):
    """Relabel every state in `_pull_cards.html` alone: both pages follow,
    so neither spells a label of its own."""
    from pathlib import Path

    from jinja2 import ChoiceLoader, DictLoader

    import mimir.linus_pulls as lp
    import mimir.merge_window as mw

    held, _ = bodies
    _seed_four_states(seeded_db, mirror, held)
    _fill(seeded_db, _v73())
    include = (
        Path(history.__file__).parent / "templates" / "_pull_cards.html"
    ).read_text()
    relabels = {
        "'Waiting'": "'L-pending'",
        "'Merged'": "'L-merged'",
        "'Unclear'": "'L-unknown'",
        "'Rejected'": "'L-rejected'",
        "'Not merged in '": "'L-not-merged '",
    }
    for old, new in relabels.items():
        assert include.count(old) == 1, old
        include = include.replace(old, new)
    app = client.application
    app.jinja_loader = ChoiceLoader(
        [DictLoader({"_pull_cards.html": include}), app.jinja_loader]
    )
    # The live page counts these pulls from the last rc, as pending,
    # unclear and rejected (no tip is in Linus's tree here).
    for module in (lp, mw):
        monkeypatch.setattr(module, "version_tags", lambda path: ["v7.2", "v7.3-rc1"])
    monkeypatch.setattr(lp, "_tag_time", lambda path, tag: _TAG_TIME)
    monkeypatch.setattr(lp, "_merged_tips", lambda path, tag: set())
    from mimir.merge_window import MERGE_WINDOW_PATH

    live = _cards(client.get(MERGE_WINDOW_PATH).get_data(as_text=True))
    closed = _cards(_page(client))
    assert {k: label for k, (label, _) in live.items()} == {
        "pending": "L-pending",
        "unknown": "L-unknown",
        "rejected": "L-rejected",
    }
    assert {k: label for k, (label, _) in closed.items()} == {
        "merged": "L-merged",
        "not_merged": "L-not-merged v7.3",
        "unknown": "L-unknown",
        "rejected": "L-rejected",
    }


def test_rows_resolve_subject_author_and_link_at_render_time(
    client, seeded_db, mirror, bodies
):
    """The cached row holds only the article id and its state, so a
    subject, an author's privacy and the canonical inbox are today's."""
    from sqlalchemy import select

    from mimir.models import Article, Inbox

    held, _ = bodies
    with seeded_db() as s:
        art = _pull(s, held, "moved@x", mirror["new"], hours=48)
        s.commit()
        article_id = art.id
    _fill(seeded_db, _v73())
    assert f'href="/alpha/2026/10/{article_id}"' in _page(client)
    with seeded_db() as s:
        art = s.get(Article, article_id)
        art.subject = "[GIT PULL] renamed subject"
        beta = s.scalar(select(Inbox).where(Inbox.name == "beta"))
        art.lists[0].inbox_id = beta.id
        s.commit()
    html = _page(client)
    assert "renamed subject" in html
    assert f'href="/beta/2026/10/{article_id}"' in html


def test_a_page_view_on_a_miss_never_classifies_and_shows_no_section(
    client, seeded_db, mirror, bodies, monkeypatch
):
    held, reads = bodies
    _seed_four_states(seeded_db, mirror, held)
    monkeypatch.setattr(
        history, "calculate_pulls", lambda *a: pytest.fail("a request classified")
    )
    html = _page(client)
    assert parse(html).find_all("section", cls="pull-card") == []
    assert 'id="pull-requests-to-linus"' not in html
    assert reads == []
    assert cache.get(_v73().key + history.PULLS) is None


def test_page_two_has_no_pull_section(client, seeded_db, mirror, bodies, monkeypatch):
    held, _ = bodies
    _seed_four_states(seeded_db, mirror, held)
    _fill(seeded_db, _v73())
    monkeypatch.setattr(history, "PAGE_SIZE", 1)
    # The release's only archived reference fills page 1; page 2 needs two.
    _record_reference(seeded_db, mirror["new"], "art3@example.com")
    _record_reference(seeded_db, mirror["new"], "art4@example.com")
    assert 'id="pull-requests-to-linus"' in _page(client)
    assert 'id="pull-requests-to-linus"' not in _page(client, page=2)


def _record_reference(seeded_db, sha, message_id):
    from datetime import UTC, datetime

    from mimir.models import MainlineCommit

    with seeded_db() as s:
        s.add(
            MainlineCommit(
                commit_sha=sha,
                message_id=message_id,
                tree_name="linus",
                committed_at=datetime.now(UTC),
            )
        )
        s.commit()


def _note(html):
    found = parse(html).find_all("p", id="pulls-how-counted")
    return found[0].text() if found else None


_COVERAGE = (
    "None of these pull requests names its last commit",
    "pr-tracker-bot did not reply to any of these",
    "no pr-tracker-bot reply",
)


@pytest.mark.parametrize(
    ("summary", "says"),
    [
        # Before 2012: no tip, no bot. Everything is Unclear, and why.
        ({"count": 3, "unclear": 3, "tips": 0, "bot_replies": 0}, _COVERAGE[0]),
        # No tip and no bot, but Linus refused one: not "all are Unclear"
        # (review finding, #1135).
        ({"count": 3, "unclear": 2, "tips": 0, "bot_replies": 0}, "2 name none"),
        # 2012 to 2018: tips, no bot.
        ({"count": 10, "unclear": 4, "tips": 6, "bot_replies": 0}, _COVERAGE[1]),
        # The bot replies to some: a share past the threshold still says so.
        ({"count": 10, "unclear": 2, "tips": 8, "bot_replies": 3}, "2 of 10 name"),
        # One of them: singular.
        ({"count": 5, "unclear": 1, "tips": 4, "bot_replies": 3}, "1 of 5 names no"),
        (
            {"count": 5, "unclear": 1, "tips": 4, "bot_replies": 0},
            "1 names none and is",
        ),
        # At or under the threshold: the counting rule only.
        ({"count": 100, "unclear": 10, "tips": 90, "bot_replies": 90}, None),
    ],
)
def test_coverage_note_is_decided_from_the_release_data(
    client, seeded_db, mirror, bodies, summary, says
):
    held, _ = bodies
    _seed_four_states(seeded_db, mirror, held)
    release = _v73()
    _fill(seeded_db, release)
    cache.set(release.key + history.PULLS, summary, history.GIT_TTL_SEC)
    note = _note(_page(client))
    assert note is not None and "14 days before v7.2" in note
    assert "made it in or Linus rejected them by the tag" in note
    assert sum(c in note for c in _COVERAGE) == (1 if says else 0), note
    if says:
        assert says in note, note


def test_coverage_threshold_is_ten_percent():
    assert history.UNCLEAR_NOTE_SHARE == 0.10


def _fake(n):
    """Releases newest first with cached Git facts and membership, as the
    per-release warm targets leave them. Keys are made up."""
    out = []
    facts = history.ReleaseFacts(
        release_date=datetime(2020, 1, 1, tzinfo=UTC),
        date_source=None,
        merge_days=None,
        stabilization_days=None,
        rc_count=0,
        commit_count=1,
    )
    for i in range(n):
        k = n - i
        previous = (f"v9.{k - 1}", "1" * 40) if k > 1 else (None, None)
        release = history.Release(f"v9.{k}", None, "0" * 40, *previous, (), f"fake:{k}")
        cache.set(release.key + ":facts", facts.model_dump(mode="json"), 3600)
        cache.set(release.key + ":members", [f"m{n - i}"], 3600)
        out.append(release)
    return out


def _clocked(monkeypatch, step, fail=()):
    now = [0.0]
    asked = []
    monkeypatch.setattr(history.time, "monotonic", lambda: now[0])

    def stub(session, release, members, previous_members):
        now[0] += step
        asked.append(release.tag)
        if release.tag in fail:
            raise RuntimeError("boom")
        return history.ReleasePullSummary(count=1, unclear=0, tips=1, bot_replies=0), [
            [1, "merged"]
        ]

    monkeypatch.setattr(history, "calculate_pulls", stub)
    return asked


def test_the_warm_fills_missing_pulls_newest_first_within_its_budget(
    seeded_db, monkeypatch
):
    releases = _fake(4)
    asked = _clocked(monkeypatch, step=10)
    with seeded_db() as s:
        history.warm_release_pulls(s, releases, budget_sec=15)
        assert asked == ["v9.4", "v9.3"]
        asked.clear()
        history.warm_release_pulls(s, releases, budget_sec=15)
        assert asked == ["v9.2", "v9.1"]
        asked.clear()
        history.warm_release_pulls(s, releases, budget_sec=15)
        assert asked == []
    assert all(cache.get(r.key + history.PULLS) for r in releases)
    assert all(cache.get(r.key + history.PULL_ROWS) for r in releases)


def test_the_pull_fill_waits_for_git_facts_and_skips_a_rangeless_release(
    seeded_db, monkeypatch
):
    releases = _fake(3)
    cache.delete(releases[0].key + ":members")
    cache.set(releases[1].key + ":members", None, 3600)
    asked = _clocked(monkeypatch, step=1)
    with seeded_db() as s:
        history.warm_release_pulls(s, releases, budget_sec=100)
    assert asked == ["v9.1"]


def test_the_pull_fill_waits_for_the_previous_releases_membership(
    seeded_db, monkeypatch
):
    """Deciding the lead-in needs vPREV's members: without them a release
    waits for the next pass, as the subsystem fill waits for its links."""
    releases = _fake(3)
    cache.delete(releases[1].key + ":members")
    asked = _clocked(monkeypatch, step=1)
    with seeded_db() as s:
        history.warm_release_pulls(s, releases, budget_sec=100)
    # v9.3 waits for v9.2's members, v9.2 for its own; v9.1 is the oldest.
    assert asked == ["v9.1"]


def test_the_oldest_release_keeps_its_whole_lead_in(seeded_db, mirror, bodies):
    """v7.2's predecessor v7.1 has no Git range, so it has no page to hand
    a lead-in pull to: v7.2 keeps them all, rejected ones included."""
    held, _ = bodies
    # v7.2's lead-in is the two weeks before v7.1's tag.
    lead_in = -_HOURS_TO_V73 - 48
    with seeded_db() as s:
        _pull(s, held, "lead-in-refused@x", _TIP_B, hours=lead_in)
        _linus_reply(
            s, held, "lead-in-refused@x", "Not pulling this.", hours=lead_in + 24
        )
        _pull(s, held, "lead-in-old@x", mirror["old"], hours=lead_in)
        s.commit()
    v72 = _release("v7.2")
    _fill(seeded_db, v72)
    assert history._cached_git(_release("v7.1"))[1] is None
    assert _states(v72) == {
        "lead-in-refused@x": "rejected",
        "lead-in-old@x": "merged",
    }


def test_a_release_whose_pulls_fail_does_not_block_the_older_ones(
    seeded_db, monkeypatch
):
    releases = _fake(3)
    asked = _clocked(monkeypatch, step=1, fail=("v9.3",))
    with seeded_db() as s:
        history.warm_release_pulls(s, releases, budget_sec=100)
    assert asked == ["v9.3", "v9.2", "v9.1"]
    assert [bool(cache.get(r.key + history.PULLS)) for r in releases] == [
        False,
        True,
        True,
    ]


def test_pulls_survive_their_expiry_under_hourly_warm_passes(
    seeded_db, mirror, bodies, monkeypatch
):
    """`:pulls` is renewed with the Git facts on every warm pass, never
    recalculated (#1131's hourly-step pattern)."""
    import random

    held, _ = bodies
    _seed_four_states(seeded_db, mirror, held)
    release = _v73()
    now = cache._now()
    clock = [now]
    monkeypatch.setattr(cache, "_now", lambda: clock[0])
    window = 7200
    with cache.warm_window(window):
        _fill(seeded_db, release)
    keys = [release.key + history.PULLS, release.key + history.PULL_ROWS]
    assert set(cache.get_many(keys)) == set(keys)
    monkeypatch.setattr(
        history,
        "calculate_pulls",
        lambda *a: pytest.fail("renewing immutable pulls must not reclassify"),
    )
    monkeypatch.setattr(random, "random", lambda: 0.0)
    hours = (history.GIT_TTL_SEC + 3 * window) // 3600
    for hour in range(1, hours + 1):
        clock[0] = now + hour * 3600
        with cache.warm_window(window), seeded_db() as session:
            history.warm_release(session, release)
            history.warm_release_pulls(session, history.release_catalog())
        assert set(cache.get_many(keys)) == set(keys), f"lapsed at hour {hour}"
    assert hours * 3600 > history.GIT_TTL_SEC + window


def test_the_slow_tier_fills_pulls_after_the_per_release_targets(monkeypatch):
    from mimir.cli.cache import _build_slow_global_targets

    releases = [
        history.Release(f"v9.{n}", None, "0" * 40, None, None, (), f"k{n}")
        for n in (2, 1)
    ]
    monkeypatch.setattr(history, "release_catalog", lambda: releases)
    names = [name for name, _ in _build_slow_global_targets("http://x")]
    assert names[:2] == ["release_history:v9.2", "release_history:v9.1"]
    assert names[-2:] == ["release_history:subsystems", "release_history:pulls"]


def _sitemap_tags(client):
    body = client.get("/meta-sitemap.xml").get_data(as_text=True)
    prefix = re.escape(history.release_history_path() + "/")
    return set(re.findall(prefix + r"(v[\d.]+)</loc>", body))


def _index_tags(client):
    body = client.get(history.release_history_path()).get_data(as_text=True)
    prefix = re.escape(history.release_history_path() + "/")
    return set(re.findall(prefix + r'(v[\d.]+)"', body)), body


def test_pulls_qualify_a_release_for_the_index_and_the_sitemap(
    client, seeded_db, mirror, bodies
):
    """#1132: a Git range and (a recorded reference or a classified pull),
    Unclear included, for both lists through the one predicate."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "unclear@x", hours=48)
        s.commit()
    release = _v73()
    history.release_data(release)
    history.git_facts(_release("v7.2"))
    # Precondition: no recorded reference, so only the pull can list it.
    assert history.cached_links([release])[release.tag].pair_count == 0
    assert _sitemap_tags(client) == set()
    tags, _ = _index_tags(client)
    assert tags == set()
    # Its facts and links are cached, but whether it is listed waits on
    # its pulls, so it is not counted as checked yet.
    assert history.recorded_history([release]).checked == 0
    with seeded_db() as s:
        history.warm_release_pulls(s, history.release_catalog())
    assert _states(release) == {"unclear@x": "unknown"}
    assert _sitemap_tags(client) == {"v7.3"}
    tags, body = _index_tags(client)
    assert tags == {"v7.3"}
    assert "recorded references or pull requests" in body
    assert history.recorded_history([release]).checked == 1


def test_a_release_with_neither_is_checked_and_not_listed(
    client, seeded_db, mirror, bodies
):
    release = _v73()
    history.release_data(release)
    history.git_facts(_release("v7.2"))
    with seeded_db() as s:
        history.warm_release_pulls(s, history.release_catalog())
    assert cache.get(release.key + history.PULLS)["count"] == 0
    assert _sitemap_tags(client) == set()
    recorded = history.recorded_history([release])
    assert (recorded.listed, recorded.checked) == ([], 1)


@pytest.mark.parametrize(
    ("pairs", "pulls", "listed"),
    [(0, None, False), (0, 0, False), (0, 1, True), (1, None, True), (1, 0, True)],
)
def test_the_predicate(pairs, pulls, listed):
    from datetime import UTC, datetime

    facts = history.ReleaseFacts(
        release_date=None,
        date_source=None,
        merge_days=None,
        stabilization_days=None,
        rc_count=0,
        commit_count=1,
    )
    links = history.ReleaseLinks(pair_count=pairs, computed_at=datetime.now(UTC))
    summary = (
        None
        if pulls is None
        else history.ReleasePullSummary(count=pulls, unclear=0, tips=0, bot_replies=0)
    )
    assert history.has_recorded_history(facts, links, summary) is listed
    rangeless = facts.model_copy(update={"commit_count": None})
    assert history.has_recorded_history(rangeless, links, summary) is False


def test_a_cached_row_whose_article_is_unlinked_or_gone_is_skipped(
    client, seeded_db, mirror, bodies
):
    """`release_pulls` resolves rows at render time; an article unlinked or deleted since the fill drops out instead of breaking the page."""
    from sqlalchemy import delete

    from mimir.models import Article, ArticleList

    held, _ = bodies
    with seeded_db() as s:
        keep = _pull(s, held, "keep@x", mirror["new"], hours=48).id
        unlinked = _pull(s, held, "unlinked@x", mirror["new"], hours=49).id
        gone = _pull(s, held, "gone@x", mirror["new"], hours=50).id
        s.commit()
    _fill(seeded_db, _v73())
    with seeded_db() as s:
        for article_id in (unlinked, gone):
            s.execute(delete(ArticleList).where(ArticleList.article_id == article_id))
        s.execute(delete(Article).where(Article.id == gone))
        s.commit()
    html = _page(client)
    assert f'/{keep}"' in html
    assert f'/{unlinked}"' not in html
    assert f'/{gone}"' not in html


def test_a_rejection_after_the_release_still_counts(seeded_db, mirror, bodies):
    """Linus's refusal is his decision whenever he sends it; only the bot's
    reply is windowed. One sent after vN's tag keeps a late pull off vN
    (it was not rejected by the tag), so the pull is vN+1's, shown there
    as Rejected."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "late-no@x", _TIP_B, hours=_HOURS_TO_V73 - 24)
        _linus_reply(
            s, held, "late-no@x", "Not pulling this.", hours=_HOURS_TO_V73 + 48
        )
        s.commit()
    v73, v74 = _release("v7.3"), _release("v7.4")
    _fill(seeded_db, v73, v74)
    assert _states(v73) == {}
    assert _states(v74) == {"late-no@x": "rejected"}


def test_a_refusal_after_the_tag_does_not_drop_a_pull_from_both_pages(
    seeded_db, mirror, bodies
):
    """vN's row is written once, on the first warm pass after its tag; vN+1's weeks later. A lead-in pull for vN+1 that Linus refuses during vN+1's merge window, after vN's row was written, must still be on exactly one page."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "early-for-v74@x", _TIP_B, hours=_HOURS_TO_V73 - 72)
        s.commit()
    v73, v74 = _release("v7.3"), _release("v7.4")
    _fill(seeded_db, v73)
    with seeded_db() as s:
        _linus_reply(
            s, held, "early-for-v74@x", "Not pulling this.", hours=_HOURS_TO_V73 + 72
        )
        s.commit()
    _fill(seeded_db, v74)
    pages = [m for release in (v73, v74) for m in _states(release)]
    assert pages == ["early-for-v74@x"]


def test_a_refusal_at_the_tag_instant_counts_by_the_tag(seeded_db, mirror, bodies):
    """ "By the tag" includes the tag's own instant, as for a bot reply."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "refused-at-tag@x", _TIP_B, hours=_HOURS_TO_V73 - 72)
        _linus_reply(
            s, held, "refused-at-tag@x", "Not pulling this.", hours=_HOURS_TO_V73
        )
        s.commit()
    v73, v74 = _release("v7.3"), _release("v7.4")
    _fill(seeded_db, v73, v74)
    assert _states(v73) == {"refused-at-tag@x": "rejected"}
    assert _states(v74) == {}


def test_a_bot_reply_by_the_tag_keeps_a_pull_whose_tip_is_elsewhere(
    seeded_db, mirror, bodies
):
    """A re-pushed tag: the named tip is in no release, but pr-tracker-bot replied before vN's tag that it was merged. It made it into vN, as `_state` already says, so it is vN's and not vN+1's."""
    held, _ = bodies
    with seeded_db() as s:
        _pull(s, held, "repushed@x", _TIP_B, hours=_HOURS_TO_V73 - 72)
        _bot_reply(s, held, "repushed@x", hours=_HOURS_TO_V73 - 24)
        s.commit()
    v73, v74 = _release("v7.3"), _release("v7.4")
    _fill(seeded_db, v73, v74)
    assert _states(v73) == {"repushed@x": "merged"}
    assert _states(v74) == {}


def test_the_fill_waits_a_day_after_a_tag(seeded_db, monkeypatch):
    """Replies dated by the tag can be ingested a little later, so a release
    is classified only once its tag is PULLS_SETTLE old: on a later pass."""
    releases = _fake(2)
    tagged = datetime(2026, 11, 1, 12, tzinfo=UTC)
    facts = cache.get(releases[0].key + ":facts") | {"release_date": tagged.isoformat()}
    cache.set(releases[0].key + ":facts", facts, 10**9)
    asked = _clocked(monkeypatch, step=1)
    # A day, written out: the test must not move with the constant.
    settled = tagged + timedelta(days=1)
    for now, expected in (
        (settled - timedelta(seconds=1), ["v9.1"]),
        (settled, ["v9.2"]),
    ):
        monkeypatch.setattr(cache, "_now", lambda now=now: int(now.timestamp()))
        asked.clear()
        with seeded_db() as s:
            history.warm_release_pulls(s, releases, budget_sec=100)
        assert asked == expected, now
