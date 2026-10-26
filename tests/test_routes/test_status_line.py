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


def _add_v2_repost(seeded):
    """A second v2 of the same patch, newer than `v2@x` and filed in
    alpha, so it is a re-post: the slot's patch stays `v2@x` (lowest id,
    no diff in either), while the pill, which ranks by date, names this
    one. Returns its id."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        late = Article(
            message_id="v2-late@x",
            subject="[PATCH v2 1/1] foo: bar",
            subject_normalized="foo: bar",
            date=datetime(2024, 1, 9, tzinfo=UTC),
            patch_series_key=KEY,
            patch_series_version="v2",
            patch_series_position=1,
            lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
        )
        s.add(late)
        s.commit()
        return late.id


def test_superseded_link_names_the_copy_the_pill_dated(client, tmp_path):
    """#861: a v2 posted again gives the revisions timeline one entry,
    the slot's patch (`v2@x`, lowest id). The pill and its "(posted ...)"
    date name the newest copy, so the link must go there too, and a new
    re-post moves the ETag."""
    seeded = _seed(tmp_path, v2=True)
    url = seeded["p@x"][1] + "/t"
    before = client.get(url)
    patch_id, patch_url = seeded["v2@x"]
    assert _status(before.get_data(as_text=True)) == (
        "Superseded by v2 (posted 2024-01-08).",
        patch_url,
    )  # precondition: one v2, the link names it
    late_id = _add_v2_repost(seeded)
    assert late_id > patch_id  # precondition: the winner is the lower id

    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    sentence, link = _status(after.get_data(as_text=True))
    assert sentence == "Superseded by v2 (posted 2024-01-09)."
    assert link.endswith(f"/{late_id}"), link
    assert link != patch_url


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


def test_two_newest_copies_with_one_date_name_the_higher_id(client, tmp_path):
    """The pill ranks the copies of the newer version by date. Two copies
    sharing the newest date used to leave the pick to the scan order, so
    the pill's id (and the status line's link) could change between runs;
    the higher id wins."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.lifecycle_status import _bulk_uncached
    from mimir.models import Inbox

    seeded = _seed(tmp_path, v2=True)
    p_id = seeded["p@x"][0]
    with SessionLocal() as s:
        tied = [
            Article(
                message_id=f"v2-tie-{n}@x",
                subject="[PATCH v2 1/1] foo: bar",
                subject_normalized="foo: bar",
                date=datetime(2024, 1, 9, tzinfo=UTC),
                patch_series_key=KEY,
                patch_series_version="v2",
                patch_series_position=1,
            )
            for n in "ab"
        ]
        s.add_all(tied)
        s.commit()
        ids = sorted(a.id for a in tied)
        inbox_id = s.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        got = _bulk_uncached(s, [p_id], inbox_id)[p_id]
    assert ids[0] < ids[1]  # precondition: two copies, one lower id
    assert got.superseded_by_id == ids[1]
