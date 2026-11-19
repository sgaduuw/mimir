"""`articles.canonical_patch` replaces a Python SQL function (#1221).

A Python function called from inside `sqlite3_step` on free-threaded
CPython can free another thread's cached statement while holding its
own connection's mutex, and two threads doing that deadlock. So no SQL
in mimir may call back into Python: the recent-postings eligibility is
stored at write time instead, and every path that writes the subject or
the series fields must keep it true.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

import mimir
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_subjects import _is_patch_subject, _is_reply_to_patch
from mimir.subsystems_dashboard.recent import recent_postings_sql
from tests.test_subsystems._helpers import _add_subsystem

# (subject, position, key). Covers, in-series patches, solo and
# list-tagged patches, replies, and cover or in-series subjects whose
# position and key ingest or the reprocess cleared (#820, #837).
CORPUS = [
    ("[PATCH v2 0/3] series title", 0, "k1"),
    ("[PATCH v2 2/3] foo: do bar", 2, "k1"),
    ("[PATCH v2 2/3] foo: do bar", 2, None),
    ("[PATCH 0/3] stored before the #212 backfill", None, "k2"),
    ("[PATCH v2 0/3] series title", None, None),
    ("[PATCH v2 2/3] foo: do bar", None, None),
    ("[PATCH] foo: solo", None, None),
    ("[PATCH net-next] foo: solo", None, None),
    ("[U-Boot] [PATCH v2] fix: do bar", None, None),
    ("[net-next v7 02/10] no PATCH token", 2, "k3"),
    ("Re: [PATCH v2 2/3] foo: do bar", None, None),
    ("[GIT PULL] urgent fixes", None, None),
    ("[ANNOUNCE] v6.1", None, None),
    ("plain prose", None, None),
    ("", None, None),
    (None, None, None),
]


def _expected(subject, position, key):
    """The predicate the removed SQL function evaluated, spelled out
    independently of the code under test."""
    row = SimpleNamespace(
        subject=subject, patch_series_position=position, patch_series_key=key
    )
    return _is_patch_subject(subject) and not _is_reply_to_patch(row)


def _stored(seeded_db, message_id):
    with seeded_db() as s:
        return s.execute(
            text("SELECT canonical_patch FROM articles WHERE message_id = :m"),
            {"m": message_id},
        ).scalar_one()


def _article(subject, mid, *, position=None, key=None, inbox_id):
    return Article(
        message_id=mid,
        subject=subject,
        author="a@example",
        date=datetime(2024, 6, 1, tzinfo=UTC),
        subject_normalized="s",
        canonical_inbox_id=inbox_id,
        patch_series_key=key,
        patch_series_version="v2" if key else None,
        patch_series_position=position,
        lists=[ArticleList(inbox_id=inbox_id, epoch="0.git", commit_sha="c" * 40)],
    )


def _alpha_id(session):
    return session.execute(select(Inbox.id).where(Inbox.name == "alpha")).scalar_one()


@pytest.mark.parametrize(
    "since, inbox_first",
    [(datetime(2024, 1, 1), False), (None, True), (None, False)],
    ids=["date-window", "inbox-first", "path-first"],
)
def test_recent_postings_sql_runs_without_python_functions(
    seeded_db, since, inbox_first
):
    """The query runs on a bare sqlite3 connection with nothing registered.
    Any Python SQL function fails here with "no such function"."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "GUARD", None, ["drivers/demo/"])
        s.commit()
        statement, params = recent_postings_sql(
            sub, since=since, inbox_first=inbox_first
        )
        inbox_id = _alpha_id(s)
        db_path = s.get_bind().url.database
    if since is not None:
        params["since"] = since.isoformat(sep=" ")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            str(statement),
            {**params, "inbox_id": inbox_id, "limit": 30, "preview_limit": 30},
        ).fetchall()
    finally:
        conn.close()


def test_no_python_sql_functions_are_registered():
    package = Path(mimir.__file__).parent
    assert not [p for p in package.rglob("*.py") if "create_function" in p.read_text()]


def test_stored_value_matches_the_predicate_on_insert(seeded_db):
    with seeded_db() as s:
        inbox_id = _alpha_id(s)
        for i, (subject, position, key) in enumerate(CORPUS):
            s.add(
                _article(
                    subject, f"p{i}@x", position=position, key=key, inbox_id=inbox_id
                )
            )
        s.commit()
    got = [_stored(seeded_db, f"p{i}@x") for i in range(len(CORPUS))]
    assert got == [_expected(*row) for row in CORPUS]
    # The corpus must exercise both answers and the cleared-reply rule.
    assert set(got) == {0, 1}
    assert _expected("[PATCH v2 0/3] series title", 0, "k1")
    assert not _expected("[PATCH v2 0/3] series title", None, None)


def test_batches_yield_exactly_the_true_ids_across_batch_boundaries(seeded_db):
    from mimir.patch_subjects import canonical_patch_batches

    with seeded_db() as s:
        inbox_id = _alpha_id(s)
        s.execute(text("DELETE FROM articles"))
        for i, (subject, position, key) in enumerate(CORPUS):
            s.add(
                _article(
                    subject, f"b{i}@x", position=position, key=key, inbox_id=inbox_id
                )
            )
        s.commit()
        ids = dict(
            s.execute(
                text("SELECT message_id, id FROM articles WHERE message_id LIKE 'b%'")
            ).all()
        )
        want = sorted(ids[f"b{i}@x"] for i, row in enumerate(CORPUS) if _expected(*row))
        conn = s.connection()
        got = [
            i for batch in canonical_patch_batches(conn, batch_size=2) for i in batch
        ]
    assert got == want


def test_orm_update_recomputes_when_subject_or_series_fields_change(seeded_db):
    with seeded_db() as s:
        art = _article("plain prose", "u1@x", inbox_id=_alpha_id(s))
        s.add(art)
        s.commit()
        assert _stored(seeded_db, "u1@x") == 0

        art.subject = "[PATCH v2 0/3] series title"
        art.patch_series_position = 0
        art.patch_series_key = "k"
        s.commit()
        assert _stored(seeded_db, "u1@x") == 1

        # A reply the reprocess clears: same subject, no position or key.
        art.patch_series_position = None
        art.patch_series_key = None
        s.commit()
        assert _stored(seeded_db, "u1@x") == 0


def test_orm_update_skips_the_predicate_when_inputs_are_unchanged(
    seeded_db, monkeypatch
):
    import mimir.patch_subjects

    with seeded_db() as s:
        art = _article("[PATCH] solo", "u2@x", inbox_id=_alpha_id(s))
        s.add(art)
        s.commit()
        calls = []
        real = mimir.patch_subjects.canonical_patch
        monkeypatch.setattr(
            mimir.patch_subjects,
            "canonical_patch",
            lambda *a: calls.append(a) or real(*a),
        )
        art.date = datetime(2025, 1, 1, tzinfo=UTC)
        s.commit()
        assert calls == []
        art.subject = "Re: [PATCH] solo"
        s.commit()
        assert len(calls) == 1
    assert _stored(seeded_db, "u2@x") == 0


# --- Write paths -----------------------------------------------------------


INGEST_SUBJECTS = {
    "solo@x": ("[PATCH] foo: solo", 1),
    "inseries@x": ("[PATCH 1/2] foo: one", 1),
    "reply@x": ("Re: [PATCH] foo: solo", 0),
    "prose@x": ("plain prose", 0),
}


def _messages():
    from tests.test_ingest._helpers import _rfc5322, _subject

    return [
        _subject(_rfc5322(mid), subject)
        for mid, (subject, _) in INGEST_SUBJECTS.items()
    ]


def test_live_ingest_stores_the_value(seeded_db, tmp_path, broker_active):
    from mimir.ingest import ingest_epoch
    from tests.test_ingest._helpers import _alpha, _build_pubinbox_repo

    alpha = _alpha(seeded_db)
    repo = _build_pubinbox_repo(tmp_path / "0.git", _messages())
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo, workers=1)
    got = {mid: _stored(seeded_db, mid) for mid in INGEST_SUBJECTS}
    assert got == {mid: want for mid, (_, want) in INGEST_SUBJECTS.items()}


def test_replay_stores_the_value(seeded_db, tmp_path, monkeypatch, broker_active):
    import mimir.parser
    from mimir.ingest import ingest_epoch, replay_failures
    from tests.test_ingest._helpers import _alpha, _build_pubinbox_repo

    alpha = _alpha(seeded_db)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(mirror / "0.git", _messages())
    with seeded_db() as s:
        s.get(Inbox, alpha.id).mirror_path = str(mirror)
        s.commit()
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 10)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror / "0.git", workers=1)
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)
    with seeded_db() as s:
        inbox = s.get(Inbox, alpha.id)
    assert replay_failures(inbox).recovered == len(INGEST_SUBJECTS)
    got = {mid: _stored(seeded_db, mid) for mid in INGEST_SUBJECTS}
    assert got == {mid: want for mid, (_, want) in INGEST_SUBJECTS.items()}


def test_reprocess_clears_the_value_on_a_reply_stored_as_a_cover(
    seeded_db, broker_active
):
    """A reviewer's reply repeating a cover subject was stored as a cover
    (#820). The reprocess clears its position and key, so it must also
    stop being a canonical patch."""
    from mimir.patch_series import backfill_patch_series, series_key

    cover = "[PATCH v2 0/2] improve foo handling"
    with seeded_db() as s:
        inbox_id = _alpha_id(s)
        s.add(
            _article(
                cover,
                "c@x",
                position=0,
                key=series_key("improve foo handling", "a@example"),
                inbox_id=inbox_id,
            )
        )
        reply = _article(
            cover,
            "r@x",
            position=0,
            key=series_key("improve foo handling", "rev@x"),
            inbox_id=inbox_id,
        )
        reply.author = "rev@x"
        reply.thread_parent = "c@x"
        s.add(reply)
        s.commit()
    assert (_stored(seeded_db, "c@x"), _stored(seeded_db, "r@x")) == (1, 1)

    backfill_patch_series(reprocess=True)

    assert (_stored(seeded_db, "c@x"), _stored(seeded_db, "r@x")) == (1, 0)


def test_reprocess_repairs_a_stale_value_when_the_series_fields_hold(
    seeded_db, broker_active
):
    """A plain run skips a fully resolved row unless something it would
    write differs, so the stored value counts as something it writes."""
    from mimir.patch_series import backfill_patch_series, series_key

    with seeded_db() as s:
        s.add(
            _article(
                "[PATCH v2 0/2] improve foo handling",
                "stale@x",
                position=0,
                key=series_key("improve foo handling", "a@example"),
                inbox_id=_alpha_id(s),
            )
        )
        s.commit()
        s.execute(
            text("UPDATE articles SET canonical_patch = 0 WHERE message_id = 'stale@x'")
        )
        s.commit()

    backfill_patch_series()

    assert _stored(seeded_db, "stale@x") == 1
