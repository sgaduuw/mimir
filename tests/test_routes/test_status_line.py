"""The status line: the lifecycle state with its evidence (#844 Part 3).

It reads the pill's own `LifecycleStatusInfo`, so the two cannot name a
different state, commit or revision. Every check here parses the pill
and the line out of the same response and compares them.
"""

import html as htmllib
import re
from datetime import UTC, datetime

import pytest

from mimir.models import Article, ArticleTrailer
from tests.test_routes._helpers import _seed_mainline_commit, seed_thread_shape

KEY = "5a" * 20
LINUS_SHA = "1" * 40
NEXT_SHA = "2" * 40


def _seed(tmp_path, *, v2=False):
    """A one-patch thread (`p@x`, v1) with a reply, and optionally a v2
    of the same patch in its own thread. Returns `seed_thread_shape`'s
    map."""
    from mimir.extensions import SessionLocal

    edges = [("p@x", None), ("re@x", "p@x")] + ([("v2@x", None)] if v2 else [])
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    with SessionLocal() as s:
        for mid, version in (("p@x", "v1"), ("v2@x", "v2")):
            if mid not in seeded:
                continue
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH {version} 1/1] foo: bar"
            a.patch_series_key = KEY
            a.patch_series_version = version
            a.patch_series_position = 1
            if version == "v2":
                # Ingest clamps a Date header to the commit time, so the
                # posting date is set here.
                a.date = datetime(2024, 1, 8, tzinfo=UTC)
        s.commit()
    return seeded


def _add_review(article_id):
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        s.add(
            ArticleTrailer(
                article_id=article_id,
                role="Reviewed-by",
                name="Rev Iewer",
                address="rev@example.org",
                address_normalized="rev@example.org",
            )
        )
        s.commit()


def _pill(html):
    """The lifecycle pill's state, or None when there is none (pending)."""
    states = re.findall(r'class="badge badge-lifecycle-(\w+)"', html)
    assert len(states) <= 1, states
    return states[0] if states else None


def _status(html):
    """The status line's sentence and its link, or (None, None)."""
    lines = re.findall(
        r'<p class="status-line"><small><strong>Status:</strong> (.*?)</small></p>',
        html,
        re.DOTALL,
    )
    assert len(lines) <= 1, lines
    if not lines:
        return None, None
    hrefs = re.findall(r'href="([^"]*)"', lines[0])
    text = re.sub(r"\s*<a [^>]*>.*?</a>", "", lines[0]).strip()
    return htmllib.unescape(text), htmllib.unescape(hrefs[0]) if hrefs else None


# state: (pill state, status sentence). A net-next commit older than the
# Linus one is seeded for "landed": the line names the Linus commit, as
# the pill does, never the earliest landing.
STATES = {
    "landed": ("landed", f"Landed in mainline as {LINUS_SHA[:12]} on 2024-06-20."),
    "superseded": ("superseded", "Superseded by v2 (posted 2024-01-08)."),
    "queued": ("queued", f"Queued in net-next as {NEXT_SHA[:12]} on 2024-06-01."),
    "reviewed": ("reviewed", "Reviewed."),
    "pending": (None, None),
}


def _make(tmp_path, state):
    seeded = _seed(tmp_path, v2=state == "superseded")
    if state in ("landed", "queued"):
        _seed_mainline_commit(
            "p@x", NEXT_SHA, "net-next", datetime(2024, 6, 1, tzinfo=UTC)
        )
    if state == "landed":
        _seed_mainline_commit(
            "p@x", LINUS_SHA, "linus", datetime(2024, 6, 20, tzinfo=UTC)
        )
    if state == "reviewed":
        _add_review(seeded["p@x"][0])
    return seeded


def test_every_lifecycle_state_is_parametrised():
    """AGENTS.md check 3, for a parametrised test: the agreement test
    below runs once per entry of STATES, so STATES must be every state
    the pill has. A state added later fails here until it is covered."""
    from mimir.lifecycle_status import LifecycleStatus

    assert set(STATES) == {s.value for s in LifecycleStatus}


@pytest.mark.parametrize("surface", ["thread", "message"])
@pytest.mark.parametrize("state", list(STATES))
def test_the_status_line_states_what_the_pill_states(client, tmp_path, state, surface):
    seeded = _make(tmp_path, state)
    url = seeded["p@x"][1] + ("/t" if surface == "thread" else "")
    html = client.get(url).get_data(as_text=True)
    want_pill, want_line = STATES[state]
    assert _pill(html) == want_pill  # precondition: the seed built the state
    assert _status(html)[0] == want_line


def test_the_markdown_thread_carries_the_status_sentence(client, tmp_path):
    seeded = _make(tmp_path, "superseded")
    md = client.get(seeded["p@x"][1] + "/t.md").get_data(as_text=True)
    assert re.search(
        r"^- Superseded by v2 \(posted 2024-01-08\)\.$", md, re.MULTILINE
    ), md


def test_superseded_links_the_newer_revision(client, tmp_path):
    seeded = _make(tmp_path, "superseded")
    html = client.get(seeded["p@x"][1] + "/t").get_data(as_text=True)
    _, link = _status(html)
    assert link == seeded["v2@x"][1]
    assert client.get(link).status_code == 200


def test_tied_trees_name_the_same_tree_on_pill_and_line(client, tmp_path):
    """#673: a subsystem tree's commit reaches linux-next with the same
    sha and committer date. linux-next recorded first is the order that
    used to pick it. The pill and the line both say net-next."""
    seeded = _seed(tmp_path)
    at = datetime(2024, 6, 1, tzinfo=UTC)
    _seed_mainline_commit("p@x", NEXT_SHA, "linux-next", at)
    _seed_mainline_commit("p@x", NEXT_SHA, "net-next", at)
    html = client.get(seeded["p@x"][1] + "/t").get_data(as_text=True)
    assert re.search(r'badge-lifecycle-queued"[^>]*>\s*IN NET-NEXT', html), html
    assert _status(html)[0] == f"Queued in net-next as {NEXT_SHA[:12]} on 2024-06-01."


@pytest.mark.parametrize("change", ["landed", "superseded"])
def test_a_status_change_moves_the_etag_and_the_line(client, tmp_path, change):
    """AGENTS.md: the validator carries every input of the body. Primed
    with a normal request, then a Linus commit or a v2 arrives: the next
    conditional request is a 200 under a new ETag showing the new line."""
    from mimir.extensions import SessionLocal

    seeded = _seed(tmp_path)
    url = seeded["p@x"][1] + "/t"
    before = client.get(url)
    assert _status(before.get_data(as_text=True))[0] is None  # pending

    if change == "landed":
        _seed_mainline_commit(
            "p@x", LINUS_SHA, "linus", datetime(2024, 6, 20, tzinfo=UTC)
        )
        want = f"Landed in mainline as {LINUS_SHA[:12]} on 2024-06-20."
    else:
        with SessionLocal() as s:
            s.add(
                Article(
                    message_id="v2-late@x",
                    subject="[PATCH v2 1/1] foo: bar",
                    subject_normalized="foo: bar",
                    date=datetime(2024, 1, 9, tzinfo=UTC),
                    patch_series_key=KEY,
                    patch_series_version="v2",
                    patch_series_position=1,
                )
            )
            s.commit()
        want = "Superseded by v2 (posted 2024-01-09)."

    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    assert _status(after.get_data(as_text=True))[0] == want


def _add_v2_copy(message_id, day, *, diff=False):
    """Another v2 of the same patch, filed in alpha so it is a re-post of
    the slot (or its patch, when it wins). `diff` gives it an
    `article_files` row. Returns its id."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleFile, ArticleList, Inbox

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        copy = Article(
            message_id=message_id,
            subject="[PATCH v2 1/1] foo: bar",
            subject_normalized="foo: bar",
            date=datetime(2024, 1, day, tzinfo=UTC),
            patch_series_key=KEY,
            patch_series_version="v2",
            patch_series_position=1,
            lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
            files=[ArticleFile(path="fs/foo.c")] if diff else [],
        )
        s.add(copy)
        s.commit()
        return copy.id


def _timeline_v2(article_id):
    """The revisions timeline's v2 entry for the article's slot, as the
    thread page builds it."""
    from mimir.extensions import SessionLocal
    from mimir.patch_state import _series_rows, _series_timeline

    with SessionLocal() as s:
        article = s.get(Article, article_id)
        rows = _series_rows(s, article)
        (entry,) = [
            e for e in _series_timeline(article, "alpha", rows) if e.version == "v2"
        ]
    return entry


@pytest.mark.parametrize(
    ("copies", "winner"),
    [
        # No copy has a diff: the lowest id, which is the oldest posting.
        ([("late@x", 9, False)], "v2@x"),
        # The lowest id has no diff and a later copy has: that one.
        ([("mid@x", 9, True), ("late@x", 10, False)], "mid@x"),
        # A later id dated before the lowest: the id decides, not the date.
        ([("early@x", 7, False)], "v2@x"),
    ],
    ids=["lowest-id", "the-copy-with-a-diff", "id-not-date"],
)
def test_pill_timeline_and_link_name_the_slot_winner(client, tmp_path, copies, winner):
    """#949: a v2 posted again is one slot, and the slot rule (#861) picks
    its patch. The pill's superseding copy, the timeline's v2 entry and
    the status line's link name that one article, and "posted" is its
    date, not the newest re-post's."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.lifecycle_status import _bulk_uncached
    from mimir.models import Inbox

    seeded = _seed(tmp_path, v2=True)
    p_id = seeded["p@x"][0]
    ids = {"v2@x": seeded["v2@x"][0]}
    for message_id, day, diff in copies:
        ids[message_id] = _add_v2_copy(message_id, day, diff=diff)
    assert winner != copies[-1][0]  # precondition: the newest copy is not the winner

    with SessionLocal() as s:
        inbox_id = s.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        info = _bulk_uncached(s, [p_id], inbox_id)[p_id]
        won = s.get(Article, ids[winner])
    assert info.superseded_by_id == ids[winner]
    v2 = _timeline_v2(p_id)
    assert v2.article_id == ids[winner]
    assert info.superseded_at.date() == won.date.date()

    html = client.get(seeded["p@x"][1] + "/t").get_data(as_text=True)
    sentence, link = _status(html)
    assert sentence == f"Superseded by v2 (posted 2024-01-{won.date.day:02d})."
    assert link == v2.url
    assert link.endswith(f"/{ids[winner]}"), link


def test_a_new_repost_of_the_slot_moves_the_etag_not_the_pick(client, tmp_path):
    """The validator still carries every copy of the slot: a re-post
    moves the ETag, and the line keeps naming the slot's patch."""
    seeded = _seed(tmp_path, v2=True)
    url = seeded["p@x"][1] + "/t"
    before = client.get(url)
    _add_v2_copy("late@x", 9)
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    assert _status(after.get_data(as_text=True)) == (
        "Superseded by v2 (posted 2024-01-08).",
        seeded["v2@x"][1],
    )


def _status_html(app, info, series):
    from flask import render_template

    from mimir.patch_state import PatchState

    state = PatchState(
        is_patch=True, trailers=[], series=series, days_since_last_reply=None
    )
    with app.test_request_context():
        return render_template(
            "_status_line.html", lifecycle_status=info, patch_state=state
        )


@pytest.mark.parametrize(
    ("named", "want"), [(12, "/late"), (11, "/patch"), (None, "/patch")]
)
def test_the_superseded_link_follows_the_id_and_falls_back_to_the_patch(
    client, named, want
):
    """The pill names a copy by id (`/late` is a re-post, `/patch` the
    slot's patch). A row cached before the id existed has None, and the
    link falls back to the version's entry, the slot's patch."""
    from mimir.lifecycle_status import LifecycleStatusInfo
    from mimir.patch_state import StateRepost, StateSeriesEntry

    series = [
        StateSeriesEntry("v1", 1, None, "/v1", True, None),
        StateSeriesEntry(
            "v2",
            11,
            None,
            "/patch",
            False,
            "/diff",
            reposts=[StateRepost(12, None, "/late", False)],
        ),
    ]
    info = LifecycleStatusInfo(
        state_value="superseded",
        superseded_by_version="v2",
        superseded_at=datetime(2024, 1, 9, tzinfo=UTC),
        superseded_by_id=named,
    )
    assert _status(_status_html(client.application, info, series))[1] == want
