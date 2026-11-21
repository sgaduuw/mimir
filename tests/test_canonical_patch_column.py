"""`articles.canonical_patch` replaces a Python SQL function (#1221).

A Python function called from inside `sqlite3_step` on free-threaded
CPython can free another thread's cached statement while holding its
own connection's mutex, and two threads doing that deadlock. So no SQL
in mimir may call back into Python: the recent-postings eligibility is
stored at write time instead, and every path that writes the subject or
the series fields must keep it true.
"""

import ast
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import column, create_engine, event, literal, select, text

import mimir
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_subjects import is_canonical_patch
from mimir.subsystems_dashboard.recent import recent_postings_sql
from tests._helpers import PYTHON_SQL_CALLBACK
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


def _patch_row(subject, position, key):
    return SimpleNamespace(
        subject=subject, patch_series_position=position, patch_series_key=key
    )


@pytest.mark.parametrize(
    "values, expected",
    list(
        zip(
            CORPUS,
            [
                True,
                True,
                True,
                True,
                False,
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
            ],
            strict=True,
        )
    ),
)
def test_canonical_patch_classification(values, expected):
    assert is_canonical_patch(_patch_row(*values)) is expected


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


def _python_sql_uses(source):
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and ast.get_docstring(node) is not None
    }
    func_names = {"func"} | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and (node.module == "sqlalchemy" or node.module.startswith("sqlalchemy."))
        for alias in node.names
        if alias.name == "func"
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name, owner = node.attr.lower(), node.value
            if name in {
                "create_function",
                "create_aggregate",
                "create_window_function",
                "regexp_match",
            } or (
                name in {"regexp", "floor"}
                and (
                    isinstance(owner, ast.Name)
                    and owner.id in func_names
                    or isinstance(owner, ast.Attribute)
                    and owner.attr == "func"
                )
            ):
                yield node.lineno
        # SQL fragments can be passed to text(), op(), or assembled later.
        # Reserve REGEXP as a token, even in a currently harmless SQL literal.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if PYTHON_SQL_CALLBACK.search(node.value) or node.value in {
                "create_function",
                "create_aggregate",
                "create_window_function",
            }:
                yield node.lineno


def test_no_python_sql_functions_are_registered():
    package = Path(mimir.__file__).parent
    assert not [
        (p, line)
        for p in package.rglob("*.py")
        for line in _python_sql_uses(p.read_text())
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import func; query = func.floor(value)",
        "from sqlalchemy import func as sql; query = sql.FLOOR(value)",
        "from sqlalchemy.sql import func as sql; query = sql.floor(value)",
        "register = connection.create_function; register('python', 1, callback)",
        "matches = column.regexp_match; query = matches(pattern)",
        "floor_sql = func.floor; query = floor_sql(value)",
        'register = getattr(connection, "create_function"); register("python", 1, callback)',
        "import sqlalchemy as sa; query = sa.func.regexp(pattern, value)",
        "query = column.regexp_match(pattern)",
        "connection.create_aggregate('sum_python', 1, Aggregate)",
        "connection.create_window_function('window_python', 1, Window)",
        'query = text("SELECT FLOOR \\n (value) FROM articles")',
        'query = text("SELECT value ReGeXp :pattern FROM articles")',
        'query = column.op("REGEXP")(pattern)',
    ],
)
def test_guard_rejects_python_sql_function_uses(tmp_path, monkeypatch, source):
    module = tmp_path / "query.py"
    module.write_text(source)
    monkeypatch.setattr(mimir, "__file__", str(tmp_path / "__init__.py"))
    with pytest.raises(AssertionError):
        test_no_python_sql_functions_are_registered()


@pytest.mark.parametrize(
    "source",
    [
        'query = text("SELECT flooring, not_regexp FROM articles")',
        'description = "floor and regexp_match are plain words"',
        "import math; result = math.floor(value)",
        '"""A local floor (synthetic), not a SQL function."""',
    ],
)
def test_guard_allows_non_sql_words(tmp_path, monkeypatch, source):
    (tmp_path / "query.py").write_text(source)
    monkeypatch.setattr(mimir, "__file__", str(tmp_path / "__init__.py"))
    test_no_python_sql_functions_are_registered()


def test_compiled_sql_guard_rejects_implicit_floor_before_driver():
    engine = create_engine("sqlite://")
    reached_driver = []

    @event.listens_for(engine, "before_cursor_execute")
    def stop_before_driver(*args):
        reached_driver.append(True)
        raise RuntimeError("guard did not stop the statement before the driver")

    try:
        with engine.connect() as conn:
            with pytest.raises(AssertionError, match="Python SQL callback"):
                conn.execute(select(column("x") // 2.5))
        assert reached_driver == []
    finally:
        engine.dispose()


def test_compiled_sql_guard_allows_integer_division():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            assert conn.scalar(select(literal(7) // 2)) == 3
    finally:
        engine.dispose()


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
    assert got == [is_canonical_patch(_patch_row(*row)) for row in CORPUS]
    # The corpus must exercise both answers and the cleared-reply rule.
    assert set(got) == {0, 1}
    assert is_canonical_patch(_patch_row("[PATCH v2 0/3] series title", 0, "k1"))
    assert not is_canonical_patch(_patch_row("[PATCH v2 0/3] series title", None, None))


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
        want = sorted(
            ids[f"b{i}@x"]
            for i, row in enumerate(CORPUS)
            if is_canonical_patch(_patch_row(*row))
        )
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


def test_backfill_stores_the_value_of_the_unfolded_subject(seeded_db, broker_active):
    """A subject stored folded (#710) does not parse as a patch; the backfill
    writes it back unfolded, so the stored value must follow the subject it
    writes, not the one it read."""
    from mimir.patch_series import backfill_patch_series, series_key

    with seeded_db() as s:
        s.add(
            _article(
                "[PATCH v2 0/2] improve\n foo handling",
                "folded@x",
                position=0,
                key=series_key("improve foo handling", "a@example"),
                inbox_id=_alpha_id(s),
            )
        )
        s.commit()
    assert _stored(seeded_db, "folded@x") == 0

    backfill_patch_series()

    assert _stored(seeded_db, "folded@x") == 1
