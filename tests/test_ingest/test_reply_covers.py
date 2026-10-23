"""A reply that repeats a cover's subject without "Re:" is not a cover
(#820).

Reviewers on some lists answer a cover letter with its subject as is,
often with the folded tab retyped as a space. `parse_cover_letter` only
reads the subject, so each such reply used to be stored as a second
cover of its own series key.
"""

import pytest
from sqlalchemy import select

from mimir.ingest import ingest_epoch
from mimir.models import Article, Inbox
from mimir.patch_series import parse_cover_letter, series_key
from tests._helpers import series_columns
from tests.test_ingest._helpers import _build_pubinbox_repo, _rfc5322

COVER = "[PATCH v2 0/2] improve foo\thandling"
REPLY = "[PATCH v2 0/2] improve foo handling"
KEY = series_key("improve foo handling", "a@b.example")


def _msg(msgid, subject, *, sender="a@b.example", in_reply_to=None):
    raw = _rfc5322(msgid, in_reply_to=in_reply_to)
    return raw.replace(b"Subject: t\r\n", f"Subject: {subject}\r\n".encode()).replace(
        b"From: a@b.example\r\n", f"From: {sender}\r\n".encode()
    )


def _ingest(seeded_db, tmp_path, epoch, messages):
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    _build_pubinbox_repo(mirror / epoch, messages)
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(mirror)
        s.commit()
        ingest_epoch(s, alpha, epoch, mirror / epoch, workers=1)


_series = series_columns


@pytest.mark.parametrize("batches", [1, 2])
def test_a_same_subject_reply_from_another_sender_is_not_a_cover(
    seeded_db, broker_active, tmp_path, batches
):
    """Same batch (the parent is found in the pending list) and across
    batches (found by one select on message_id)."""
    # Precondition: the reply's subject alone reads as a cover.
    assert parse_cover_letter(REPLY) is not None
    cover = _msg("c820@x", COVER)
    reply = _msg("r820@x", REPLY, sender="rev@b.example", in_reply_to="c820@x")
    # A patch threaded under the reply gets no series from it either.
    patch = _msg(
        "p1-820@x",
        "[PATCH v2 1/2] foo: add bar",
        sender="rev@b.example",
        in_reply_to="r820@x",
    )
    if batches == 1:
        _ingest(seeded_db, tmp_path, "0.git", [cover, reply, patch])
    else:
        _ingest(seeded_db, tmp_path, "0.git", [cover])
        _ingest(seeded_db, tmp_path, "1.git", [reply])
        _ingest(seeded_db, tmp_path, "2.git", [patch])

    got = _series(seeded_db, "c820@x", "r820@x", "p1-820@x")
    assert got["c820@x"] == (KEY, "v2", 0, None)
    assert got["r820@x"] == (None, None, None, None)
    assert got["p1-820@x"] == (None, None, 1, None)
    with seeded_db() as s:
        covers = s.execute(
            select(Article.message_id).where(Article.patch_series_position == 0)
        ).scalars()
        assert [m for m in covers if m in ("c820@x", "r820@x")] == ["c820@x"]


def test_a_reply_to_a_reply_is_not_a_cover(seeded_db, broker_active, tmp_path):
    """The parent is judged by its subject and sender, not by what was
    stored for it, so the author answering the reviewer is a reply too."""
    _ingest(
        seeded_db,
        tmp_path,
        "0.git",
        [
            _msg("c820@x", COVER),
            _msg("r820@x", REPLY, sender="rev@b.example", in_reply_to="c820@x"),
            _msg("rr820@x", REPLY, in_reply_to="r820@x"),
        ],
    )
    got = _series(seeded_db, "c820@x", "r820@x", "rr820@x")
    assert got["r820@x"] == (None, None, None, None)
    assert got["rr820@x"] == (None, None, None, None)


def test_a_same_sender_repost_stays_a_cover_with_its_patches(
    seeded_db, broker_active, tmp_path
):
    """Guard: the sender's own re-post under the first cover is still a
    cover, and its patches link to it."""
    _ingest(
        seeded_db,
        tmp_path,
        "0.git",
        [
            _msg("c820@x", COVER),
            _msg("again820@x", REPLY, in_reply_to="c820@x"),
            _msg("p1-820@x", "[PATCH v2 1/2] foo: add bar", in_reply_to="again820@x"),
        ],
    )
    got = _series(seeded_db, "again820@x", "p1-820@x")
    assert got["again820@x"][:3] == (KEY, "v2", 0)
    assert got["p1-820@x"][:3] == (KEY, "v2", 1)


def test_the_next_revision_in_reply_to_the_last_stays_a_cover(
    seeded_db, broker_active, tmp_path
):
    """Guard: a v3 cover In-Reply-To the v2 cover, even from another
    address (a colleague taking the series over)."""
    _ingest(
        seeded_db,
        tmp_path,
        "0.git",
        [
            _msg("c820@x", COVER),
            _msg(
                "v3-820@x",
                "[PATCH v3 0/2] improve foo handling",
                sender="rev@b.example",
                in_reply_to="c820@x",
            ),
        ],
    )
    got = _series(seeded_db, "v3-820@x")
    assert got["v3-820@x"][:3] == (
        series_key("improve foo handling", "rev@b.example"),
        "v3",
        0,
    )
