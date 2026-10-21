"""Resumable backfills (#680): the broker persists each run's cursor in
`backfill_runs`, in the same WriteOp as the batch it covers, so an
interrupted run continues where it stopped instead of at the newest
article.

Every test drives the real CLI command against the session broker, so
the handler, the walker and the WriteOps all run where they run in
production. Interruptions are made two ways: a batch WriteOp that raises
inside the writer (the transaction rolls back, cursor included), and a
single broker chunk with no CLI loop behind it (a killed CLI).

Batches are shrunk to 2 articles so the 7-article corpus spans four
batches: ids[0:2], ids[2:4], ids[4:6], ids[6:7] with `ids` newest-first.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner
from sqlalchemy import delete, select, text, update

from mimir import _pending_backfill
from mimir._backfill import walk_articles
from mimir.broker.handlers import dispatch
from mimir.broker.protocol import (
    BackfillArticleFilesRequest,
    BackfillCanonicalsRequest,
    BackfillPatchSeriesRequest,
)
from mimir.cli import (
    backfill_article_files_command,
    backfill_article_trailers_command,
    backfill_patch_series_command,
)
from mimir.cli.admin.canonicals import admin_canonicals_backfill_command
from mimir.config import settings
from mimir.ingest import backfill as canonicals_backfill
from mimir.ingest import ingest_epoch
from mimir.models import (
    Article,
    ArticleFile,
    ArticleTrailer,
    BackfillRun,
    Inbox,
    InboxAddressObservation,
)
from tests.test_ingest._helpers import _build_pubinbox_repo, _clear_seed_articles

_LIST = "linux-fsdevel@vger.kernel.org"
_N = 7


def _message(i: int) -> bytes:
    """One article that every backfill derives something from: a diff
    (files), a trailer (trailers), a cover-letter subject (patch
    series) and a list address (canonicals)."""
    return (
        f"Message-ID: <rb{i}@example.com>\r\n"
        "From: a@b.example\r\n"
        f"To: {_LIST}\r\n"
        f"Subject: [PATCH v1 0/3] series {i}\r\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        "\r\n"
        "Looks good.\n\n"
        f"Reviewed-by: R{i} <r{i}@example.com>\n\n"
        f"diff --git a/fs/f{i}.c b/fs/f{i}.c\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    ).encode()


def _corpus(seeded_db, tmp_path) -> list[int]:
    """Ingest `_N` readable articles into alpha; return their ids
    newest-first, the order every backfill walks."""
    _clear_seed_articles(seeded_db)
    epoch_path = tmp_path / "0.git"
    _build_pubinbox_repo(epoch_path, [_message(i) for i in range(_N)])
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(tmp_path)
        alpha.list_address = _LIST
        s.commit()
        ingest_epoch(s, alpha, "0.git", epoch_path, workers=1)
        ids = list(s.execute(select(Article.id).order_by(Article.id.desc())).scalars())
    assert len(ids) == _N
    return ids


def _wipe(seeded_db) -> None:
    """Drop everything the four backfills derive, so a run has to
    rebuild it and two runs can be compared."""
    with seeded_db() as s:
        s.execute(delete(ArticleFile))
        s.execute(delete(ArticleTrailer))
        s.execute(delete(InboxAddressObservation))
        s.execute(
            update(Article).values(
                patch_series_key=None,
                patch_series_version=None,
                patch_series_position=None,
                canonical_inbox_id=None,
            )
        )
        s.commit()


def _snapshot(seeded_db) -> dict[str, list]:
    with seeded_db() as s:
        return {
            "article_files": sorted(
                s.execute(select(ArticleFile.article_id, ArticleFile.path)).all()
            ),
            "article_trailers": sorted(
                s.execute(
                    select(ArticleTrailer.article_id, ArticleTrailer.address)
                ).all()
            ),
            "patch_series": sorted(
                s.execute(
                    select(Article.id, Article.patch_series_key).where(
                        Article.patch_series_key.is_not(None)
                    )
                ).all()
            ),
            # Observation counts are incremented, never set, so a batch
            # applied twice shows up here as a count of 2.
            "canonicals": sorted(
                s.execute(
                    select(Article.id, Article.canonical_inbox_id).where(
                        Article.canonical_inbox_id.is_not(None)
                    )
                ).all()
            )
            + sorted(
                s.execute(
                    select(
                        InboxAddressObservation.address,
                        InboxAddressObservation.count,
                    )
                ).all()
            ),
        }


class _Poison:
    """A payload whose every attribute read raises, so the batch
    WriteOp fails inside the writer thread after its real statements
    ran, and the writer rolls the whole transaction back."""

    def __getattr__(self, name):
        raise RuntimeError("simulated batch failure")


def _fail_batch(monkeypatch, submit_name: str, on_call: int) -> dict[str, int]:
    """Poison the `on_call`-th call of a `_submit_*_batch` helper.
    Returns the live call counter."""
    real = getattr(_pending_backfill, submit_name)
    calls = {"n": 0}

    def wrapped(writer, payloads, **kwargs):
        calls["n"] += 1
        if calls["n"] == on_call:
            payloads = [*payloads, _Poison()]
        return real(writer, payloads, **kwargs)

    monkeypatch.setattr(_pending_backfill, submit_name, wrapped)
    return calls


def _small_batches(monkeypatch) -> None:
    monkeypatch.setitem(walk_articles.__kwdefaults__, "batch_size", 2)
    monkeypatch.setattr(canonicals_backfill, "_BATCH", 2)


BACKFILLS = {
    "article_files": (backfill_article_files_command, "_submit_article_files_batch"),
    "article_trailers": (
        backfill_article_trailers_command,
        "_submit_article_trailers_batch",
    ),
    "patch_series": (backfill_patch_series_command, "_submit_patch_series_batch"),
    "canonicals": (admin_canonicals_backfill_command, "_submit_canonical_batch"),
}


def _invoke(command, *args: str):
    return CliRunner().invoke(command, list(args))


def _examined(output: str) -> int:
    """This session's examined count, from the summary line."""
    m = re.search(r"\bexamined=(\d+)", output)
    assert m is not None, output
    return int(m.group(1))


def _resumed_at(output: str) -> int | None:
    m = re.search(r"resuming run started .+ at id (\d+)", output)
    return int(m.group(1)) if m else None


def _runs(seeded_db) -> list[tuple]:
    with seeded_db() as s:
        return [
            (r.name, r.params_key, r.cursor, r.examined, r.finished_at is not None)
            for r in s.execute(
                text(
                    "SELECT name, params_key, cursor, examined, finished_at "
                    "FROM backfill_runs ORDER BY id"
                )
            )
        ]


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_interrupted_reprocess_resumes_where_it_stopped(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    """A `--reprocess` run whose third batch fails, re-run, examines
    only the three articles below the two committed batches, and ends
    in the same state as a run that was never interrupted."""
    command, submit = BACKFILLS[name]
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)

    _wipe(seeded_db)
    assert _snapshot(seeded_db)[name] == []
    reference_run = _invoke(command, "--reprocess")
    assert reference_run.exit_code == 0, reference_run.output
    assert _examined(reference_run.output) == _N
    reference = _snapshot(seeded_db)
    assert reference[name], f"the {name} backfill derived nothing to compare"

    _wipe(seeded_db)
    calls = _fail_batch(monkeypatch, submit, on_call=3)
    interrupted = _invoke(command, "--reprocess")
    assert interrupted.exit_code != 0, interrupted.output
    assert calls["n"] == 3

    resumed = _invoke(command, "--reprocess")
    assert resumed.exit_code == 0, resumed.output
    assert _examined(resumed.output) == _N - 4
    assert _resumed_at(resumed.output) == ids[3]
    assert _snapshot(seeded_db) == reference


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_failed_batch_leaves_cursor_at_last_committed_batch(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    command, submit = BACKFILLS[name]
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    calls = _fail_batch(monkeypatch, submit, on_call=3)

    result = _invoke(command, "--reprocess")
    assert result.exit_code != 0, result.output
    assert calls["n"] == 3

    runs = _runs(seeded_db)
    assert len(runs) == 1
    run_name, _key, cursor, examined, finished = runs[0]
    assert run_name == f"backfill_{name}"
    assert (cursor, examined, finished) == (ids[3], 4, False)


def test_canonicals_sweep_failure_keeps_the_mid_batch_cursor(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """The canonicals walk flushes mid-batch before each promotion
    sweep. Those flushes carry the cursor too, so a sweep that fails
    after the fourth article loses nothing that was flushed."""
    ids = _corpus(seeded_db, tmp_path)
    fn = canonicals_backfill.backfill_canonicals
    # (inbox_filter, limit, reprocess, promote_every, progress_every, progress)
    monkeypatch.setattr(fn, "__defaults__", (None, None, False, 2, 1000, None))
    real_sweep = _pending_backfill._submit_promote_list_address_sweep
    sweeps = {"n": 0}

    def failing_sweep(writer):
        sweeps["n"] += 1
        if sweeps["n"] == 2:
            raise RuntimeError("simulated sweep failure")
        return real_sweep(writer)

    monkeypatch.setattr(
        _pending_backfill, "_submit_promote_list_address_sweep", failing_sweep
    )

    interrupted = _invoke(admin_canonicals_backfill_command, "--reprocess")
    assert interrupted.exit_code != 0, interrupted.output
    assert sweeps["n"] == 2

    resumed = _invoke(admin_canonicals_backfill_command, "--reprocess")
    assert resumed.exit_code == 0, resumed.output
    assert _resumed_at(resumed.output) == ids[3]
    assert _examined(resumed.output) == _N - 4


def test_killed_cli_resumes_after_the_last_chunk(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """One broker chunk with no CLI loop behind it is what a killed CLI
    leaves. The next invocation continues below that chunk."""
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)

    reply = dispatch(
        BackfillPatchSeriesRequest(rpc_id=1, reprocess=True, start="resume")
        .model_dump_json()
        .encode()
    )
    assert reply.ok is True, reply.error
    assert reply.result["partial"] is True
    assert reply.result["counters"]["examined"] == 2

    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 10)
    resumed = _invoke(backfill_patch_series_command, "--reprocess")
    assert resumed.exit_code == 0, resumed.output
    assert _resumed_at(resumed.output) == ids[1]
    assert _examined(resumed.output) == _N - 2


def test_restart_starts_from_the_newest_article(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    calls = _fail_batch(monkeypatch, "_submit_patch_series_batch", on_call=2)
    assert _invoke(backfill_patch_series_command, "--reprocess").exit_code != 0
    assert calls["n"] == 2
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[1], 2, False)]

    restarted = _invoke(
        backfill_patch_series_command, "--reprocess", "--restart", "--yes"
    )
    assert restarted.exit_code == 0, restarted.output
    assert _resumed_at(restarted.output) is None
    assert _examined(restarted.output) == _N
    # The discarded run is gone; the new one walked to the oldest id.
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


def _interrupt_with_progress(command, submit, seeded_db, tmp_path, monkeypatch):
    """Leave an unfinished run that examined two articles, the state
    `--restart` must not discard unseen. Returns the corpus ids and the
    run row as `_run_row` reads it."""
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    calls = _fail_batch(monkeypatch, submit, on_call=2)
    assert _invoke(command, "--reprocess").exit_code != 0
    assert calls["n"] == 2
    row = _run_row(seeded_db)
    assert (row[2], row[3]) == (ids[1], 2), "fixture did not build a run with progress"
    return ids, row


def _run_row(seeded_db) -> tuple:
    """The only run's (id, started_at, cursor, examined, finished)."""
    with seeded_db() as s:
        (row,) = s.execute(
            text(
                "SELECT id, started_at, cursor, examined, finished_at IS NOT NULL "
                "FROM backfill_runs"
            )
        ).all()
    return tuple(row)


def _old_run_line(output: str, verb: str) -> tuple[int, str, int | None, int]:
    """The `<verb> run N started T UTC [at id C] (E examined)` line."""
    m = re.search(
        rf"^{verb} run (\d+) started (.+) UTC(?: at id (\d+))? \((\d+) examined\)$",
        output,
        re.MULTILINE,
    )
    assert m is not None, output
    cursor = int(m.group(3)) if m.group(3) else None
    return int(m.group(1)), m.group(2), cursor, int(m.group(4))


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_restart_refuses_to_discard_a_run_with_progress(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    command, submit = BACKFILLS[name]
    ids, row = _interrupt_with_progress(
        command, submit, seeded_db, tmp_path, monkeypatch
    )
    argv = ["mimir", "some-command", "--reprocess", "--restart"]
    monkeypatch.setattr("sys.argv", argv)

    result = _invoke(command, "--reprocess", "--restart")

    assert result.exit_code != 0, result.output
    assert _run_row(seeded_db) == row
    run_id, started, cursor, examined = _old_run_line(
        result.output, "refusing to discard"
    )
    assert (run_id, cursor, examined) == (row[0], ids[1], 2)
    assert started == row[1].replace("T", " ")[:19]
    resume = re.search(r"^resume it with: (.+)$", result.output, re.MULTILINE)
    assert resume is not None, result.output
    assert resume.group(1).split() == ["mimir", "some-command", "--reprocess"]
    assert re.search(r"\bexamined=\d+", result.output) is None, "a chunk walked"


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_restart_with_yes_discards_and_says_what(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    command, submit = BACKFILLS[name]
    ids, row = _interrupt_with_progress(
        command, submit, seeded_db, tmp_path, monkeypatch
    )

    result = _invoke(command, "--reprocess", "--restart", "--yes")

    assert result.exit_code == 0, result.output
    run_id, started, cursor, examined = _old_run_line(result.output, "discarding")
    assert (run_id, cursor, examined) == (row[0], ids[1], 2)
    assert started == row[1].replace("T", " ")[:19]
    # Said before the first chunk's output, not after the walk.
    assert result.output.index("discarding run") < result.output.index("examined=")
    assert _examined(result.output) == _N
    new = _run_row(seeded_db)
    assert new[0] != row[0]
    assert new[2:] == (ids[-1], _N, 1)


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_restart_needs_no_confirmation_without_progress(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    from mimir import backfill_runs

    command, _submit = BACKFILLS[name]
    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)

    # No unfinished run: nothing to report.
    fresh = _invoke(command, "--reprocess", "--restart")
    assert fresh.exit_code == 0, fresh.output
    assert "discarding" not in fresh.output
    assert "refusing" not in fresh.output

    # An unfinished run that has examined nothing is discarded unasked.
    with seeded_db() as s:
        s.execute(text("DELETE FROM backfill_runs"))
        s.commit()
    key = backfill_runs.params_key(reprocess=True)
    backfill_runs.resolve(f"backfill_{name}", key, start="resume")
    assert _run_row(seeded_db)[3] == 0, "fixture did not build an empty run"
    empty = _invoke(command, "--reprocess", "--restart")
    assert empty.exit_code == 0, empty.output
    assert "refusing" not in empty.output
    assert _old_run_line(empty.output, "discarding")[3] == 0


def test_restart_decides_inside_the_one_writeop(seeded_db, broker_active, monkeypatch):
    """Looking the run up and deleting it are one WriteOp, so a run
    that advanced between a separate check and the delete cannot be
    discarded unseen: plain `restart` never deletes a run with progress,
    and `force_restart` is the only way to."""
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    key = backfill_runs.params_key(reprocess=True)
    backfill_runs.resolve("b", key, start="resume")
    run_id = _run_row(seeded_db)[0]
    get_active_writer().submit(
        WriteOp(label="test", fn=backfill_runs.advance(run_id, 5, 3))
    ).result(timeout=10)
    before = _run_row(seeded_db)

    labels: list[str] = []
    writer = get_active_writer()
    real_submit = writer.submit
    monkeypatch.setattr(
        writer, "submit", lambda op: (labels.append(op.label), real_submit(op))[1]
    )
    refused = backfill_runs.resolve("b", key, start="restart")
    assert _run_row(seeded_db) == before
    assert refused.run is None
    assert (refused.refused.id, refused.refused.examined) == (run_id, 3)
    assert refused.discarded is None

    forced = backfill_runs.resolve("b", key, start="force_restart")
    assert (forced.discarded.id, forced.discarded.cursor) == (run_id, 5)
    assert forced.refused is None
    assert forced.run.id != run_id
    assert labels == ["backfill_runs:b:resolve"] * 2


def test_different_inbox_filter_does_not_resume_the_other_run(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    calls = _fail_batch(monkeypatch, "_submit_canonical_batch", on_call=2)
    filtered = _invoke(
        admin_canonicals_backfill_command, "--reprocess", "--inbox", "alpha"
    )
    assert filtered.exit_code != 0, filtered.output
    assert calls["n"] == 2

    unfiltered = _invoke(admin_canonicals_backfill_command, "--reprocess")
    assert unfiltered.exit_code == 0, unfiltered.output
    assert _resumed_at(unfiltered.output) is None
    assert _examined(unfiltered.output) == _N

    resumed = _invoke(
        admin_canonicals_backfill_command, "--reprocess", "--inbox", "alpha"
    )
    assert resumed.exit_code == 0, resumed.output
    assert _resumed_at(resumed.output) == ids[1]
    assert _examined(resumed.output) == _N - 2


def test_finished_run_is_not_resumed(seeded_db, tmp_path, broker_active, monkeypatch):
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    first = _invoke(backfill_patch_series_command, "--reprocess")
    assert first.exit_code == 0, first.output
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]

    second = _invoke(backfill_patch_series_command, "--reprocess")
    assert second.exit_code == 0, second.output
    assert _resumed_at(second.output) is None
    assert _examined(second.output) == _N
    assert [r[2:] for r in _runs(seeded_db)] == [
        (ids[-1], _N, True),
        (ids[-1], _N, True),
    ]


def test_limit_leaves_the_run_unfinished_and_the_next_session_continues(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    limited = _invoke(backfill_patch_series_command, "--reprocess", "--limit", "3")
    assert limited.exit_code == 0, limited.output
    assert _examined(limited.output) == 3
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[2], 3, False)]

    resumed = _invoke(backfill_patch_series_command, "--reprocess")
    assert resumed.exit_code == 0, resumed.output
    assert _resumed_at(resumed.output) == ids[2]
    assert _examined(resumed.output) == _N - 3
    assert _run_line(resumed.output) == (_N, "finished")
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


def test_advance_never_moves_the_cursor_back_up(seeded_db, broker_active):
    """Two overlapping chunks of one run (possible with more than one
    long worker) commit in either order; the cursor keeps the lower."""
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    run = backfill_runs.resolve(
        "b", backfill_runs.params_key(reprocess=True), start="resume"
    ).run
    assert run.cursor is None
    writer = get_active_writer()
    for cursor in (5, 9):
        writer.submit(
            WriteOp(label="test", fn=backfill_runs.advance(run.id, cursor, 1))
        ).result(timeout=10)
    assert [r[2:] for r in _runs(seeded_db)] == [(5, 2, False)]


def test_cursor_is_written_inside_each_batch_writeop(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """The cursor commits in the same transaction as its batch: a run
    submits nothing but its resolve, one WriteOp per batch, and the
    finish. A separate cursor WriteOp could commit without its batch."""
    from mimir.broker._context import get_active_writer

    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    writer = get_active_writer()
    real_submit = writer.submit
    labels: list[str] = []

    def recording_submit(op):
        labels.append(op.label)
        return real_submit(op)

    monkeypatch.setattr(writer, "submit", recording_submit)
    result = _invoke(backfill_patch_series_command, "--reprocess")
    assert result.exit_code == 0, result.output
    assert labels == [
        "backfill_runs:backfill_patch_series:resolve",
        *["backfill:patch_series:batch"] * 4,
        "backfill_runs:finish",
    ]


def test_batches_without_writes_still_advance_the_run(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """Without `--reprocess`, articles that already have rows produce
    no writes. Their batches still move the cursor and the count."""
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    with seeded_db() as s:
        indexed = set(s.execute(select(ArticleFile.article_id)).scalars())
    assert indexed == set(ids), "ingest should have indexed every article"

    result = _invoke(backfill_article_files_command)
    assert result.exit_code == 0, result.output
    assert re.search(r"\bskipped=7\b", result.output), result.output
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


def test_a_discarded_run_cannot_move_or_finish_its_replacement(
    seeded_db, broker_active
):
    """With more than one long worker, a chunk of a run can still be in
    flight when another session restarts it. That chunk's `advance` and
    `finish` name the old run's id, which must not be the new run's."""
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    key = backfill_runs.params_key(reprocess=True)
    old = backfill_runs.resolve("b", key, start="resume").run
    new = backfill_runs.resolve("b", key, start="restart").run
    assert [r[2:] for r in _runs(seeded_db)] == [(None, 0, False)]

    get_active_writer().submit(
        WriteOp(label="test", fn=backfill_runs.advance(old.id, 5, 3))
    ).result(timeout=10)
    backfill_runs.finish(old.id)

    assert new.id != old.id
    assert [r[2:] for r in _runs(seeded_db)] == [(None, 0, False)]


def test_canonicals_limit_leaves_the_run_unfinished_at_the_last_examined_id(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """The canonicals walk has its own loop, so `--limit` needs its own
    pin: the cursor is the last examined id, not the one that tripped
    the limit, and the run stays open."""
    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    limited = _invoke(admin_canonicals_backfill_command, "--reprocess", "--limit", "3")
    assert limited.exit_code == 0, limited.output
    assert _examined(limited.output) == 3
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[2], 3, False)]

    resumed = _invoke(admin_canonicals_backfill_command, "--reprocess")
    assert resumed.exit_code == 0, resumed.output
    assert _resumed_at(resumed.output) == ids[2]
    assert _examined(resumed.output) == _N - 3
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


def test_a_session_that_joined_a_run_stops_when_the_run_finishes(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """Two sessions on one key share a run. When session A walks it to
    the oldest article while B waits between chunks, B's next chunk ends
    B's session; it does not start a new run at the newest article."""
    from mimir.cli._common import _broker_backfill_loop
    from mimir.patch_series import BackfillResult

    ids = _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)

    def chunk(**kwargs) -> dict:
        req = BackfillPatchSeriesRequest(rpc_id=1, **kwargs)
        reply = dispatch(req.model_dump_json().encode())
        assert reply.ok is True, reply.error
        return reply.result

    calls = {"b": 0, "a": 0}

    class Session:
        """Session B's client: its second chunk lands after A finished."""

        def call(self, req):
            calls["b"] += 1
            if calls["b"] == 2:
                # Session A joins and finishes the run while B waits.
                while True:
                    calls["a"] += 1
                    start = "resume" if calls["a"] == 1 else None
                    if not chunk(reprocess=True, start=start)["partial"]:
                        break
            return chunk(limit=req.limit, reprocess=req.reprocess, start=req.start)

    result, run = _broker_backfill_loop(
        Session(),
        BackfillPatchSeriesRequest,
        BackfillResult,
        limit=None,
        reprocess=True,
    )

    assert calls["b"] == 2
    assert calls["a"] >= 1
    assert result.examined == 2
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


def _run_line(output: str) -> tuple[int, str]:
    """The run summary line: examined in total, and finished or open."""
    m = re.search(
        r"^run started .+ UTC: (\d+) examined in total, (\w+)$", output, re.MULTILINE
    )
    assert m is not None, output
    return int(m.group(1)), m.group(2)


def test_summary_says_whether_the_run_finished(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """Exactly the remaining articles as `--limit` is the edge a
    CLI-side guess gets wrong: the session stopped at its cap and the
    run finished too."""
    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    limited = _invoke(backfill_patch_series_command, "--reprocess", "--limit", "3")
    assert _run_line(limited.output) == (3, "open")

    rest = _invoke(backfill_patch_series_command, "--reprocess", "--limit", "4")
    assert _examined(rest.output) == _N - 3
    assert _run_line(rest.output) == (_N, "finished")


def test_a_model_built_schema_never_reuses_a_run_id(tmp_path):
    """`init-db` builds the schema from the models, not the migrations,
    so the model must carry AUTOINCREMENT too."""
    from datetime import datetime

    from sqlalchemy import create_engine, insert

    from mimir.models import BackfillRun

    engine = create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    BackfillRun.__table__.create(engine)
    row = {
        "name": "b",
        "params_key": "{}",
        "examined": 0,
        "started_at": datetime(2026, 10, 11),
        "updated_at": datetime(2026, 10, 11),
    }
    with engine.begin() as conn:
        first = conn.execute(insert(BackfillRun).values(**row)).inserted_primary_key[0]
        conn.execute(delete(BackfillRun))
        second = conn.execute(insert(BackfillRun).values(**row)).inserted_primary_key[0]
    engine.dispose()
    assert (first, second) == (1, 2)


# One session per run (#745). A run whose `updated_at` is newer than
# `LIVE_RUN_SEC` is being driven; a session's first chunk may not take
# it. The tests above leave runs behind that the next command resumes
# at once, so they run with the window off; the tests below turn it on.


@pytest.fixture(autouse=True)
def _no_live_window(monkeypatch):
    from mimir import backfill_runs

    monkeypatch.setattr(backfill_runs, "LIVE_RUN_SEC", 0)


@pytest.fixture
def live_window(monkeypatch):
    from mimir import backfill_runs

    monkeypatch.setattr(backfill_runs, "LIVE_RUN_SEC", 300)
    return 300


def _set_updated_at(seeded_db, seconds_ago: int) -> None:
    from datetime import UTC, datetime, timedelta

    then = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds_ago)
    with seeded_db() as s:
        s.execute(update(BackfillRun).values(updated_at=then))
        s.commit()


def _updated_at(seeded_db):
    with seeded_db() as s:
        return s.execute(select(BackfillRun.updated_at)).scalar_one()


_STARTS = ["resume", "restart", "force_restart"]


@pytest.mark.parametrize("start", _STARTS)
def test_a_session_may_not_take_a_run_that_is_live(
    start, seeded_db, broker_active, live_window
):
    """The first session's claim makes the run look live, so a second
    session's first chunk is refused whichever start it asks for, and
    nothing changes: not the row, not its `updated_at`."""
    from mimir import backfill_runs

    key = backfill_runs.params_key(reprocess=True)
    first = backfill_runs.resolve("b", key, start="resume").run
    before = _run_row(seeded_db), _updated_at(seeded_db)

    second = backfill_runs.resolve("b", key, start=start)

    assert second.run is None
    assert second.busy is not None and second.busy.id == first.id
    assert second.discarded is None and second.refused is None
    assert 0 < second.retry_after <= live_window
    assert (_run_row(seeded_db), _updated_at(seeded_db)) == before
    # A later chunk of a running session is not a claim.
    assert backfill_runs.resolve("b", key, start=None).run.id == first.id


@pytest.mark.parametrize("start", _STARTS)
def test_a_session_may_take_a_run_once_it_has_gone_quiet(
    start, seeded_db, broker_active, live_window
):
    from mimir import backfill_runs

    key = backfill_runs.params_key(reprocess=True)
    first = backfill_runs.resolve("b", key, start="resume").run

    _set_updated_at(seeded_db, live_window - 5)
    assert backfill_runs.resolve("b", key, start=start).busy is not None

    _set_updated_at(seeded_db, live_window + 5)
    taken = backfill_runs.resolve("b", key, start=start)
    assert taken.busy is None and taken.run is not None
    assert (taken.run.id == first.id) == (start == "resume")


def test_taking_a_run_makes_it_look_live_before_its_first_batch(
    seeded_db, broker_active, live_window
):
    """A resumed run is claimed in the claim's own WriteOp. Without
    that the old `updated_at` would stay until the first batch commits,
    and a second session would walk in during that gap."""
    from mimir import backfill_runs

    key = backfill_runs.params_key(reprocess=True)
    backfill_runs.resolve("b", key, start="resume")
    _set_updated_at(seeded_db, 10 * live_window)

    assert backfill_runs.resolve("b", key, start="resume").busy is None
    assert backfill_runs.resolve("b", key, start="resume").busy is not None


def test_a_batch_that_writes_nothing_still_shows_the_run_is_alive(
    seeded_db, tmp_path, broker_active, monkeypatch, live_window
):
    """Examining rows is progress even when no row changes (a
    non-reprocess pass over articles that are all indexed), and a long
    stretch of such batches must not let the run look dead."""
    from mimir import backfill_runs

    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    # One batch per chunk, and no limit: a limit would release the run.
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)
    key = backfill_runs.params_key(reprocess=False)
    backfill_runs.resolve("backfill_article_files", key, start="resume")
    _set_updated_at(seeded_db, 10 * live_window)

    reply = dispatch(BackfillArticleFilesRequest(rpc_id=1).model_dump_json().encode())

    assert reply.ok is True, reply.error
    assert reply.result["counters"]["examined"] == 2
    assert reply.result["counters"]["skipped"] == 2
    assert (datetime.now(UTC).replace(tzinfo=None) - _updated_at(seeded_db)) < (
        timedelta(seconds=live_window)
    )


def test_a_canonicals_batch_that_writes_nothing_still_shows_the_run_is_alive(
    seeded_db, tmp_path, broker_active, monkeypatch, live_window
):
    from mimir import backfill_runs

    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    # One batch per chunk, and no limit: a limit would release the run.
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)
    with seeded_db() as s:
        # Unreadable blobs: every article is skipped, so no payload.
        s.execute(update(Inbox).values(mirror_path=str(tmp_path / "gone")))
        s.commit()
    key = backfill_runs.params_key(reprocess=True)
    backfill_runs.resolve("backfill_canonicals", key, start="resume")
    _set_updated_at(seeded_db, 10 * live_window)

    reply = dispatch(
        BackfillCanonicalsRequest(rpc_id=1, reprocess=True).model_dump_json().encode()
    )

    assert reply.ok is True, reply.error
    assert reply.result["counters"]["skipped"] == 2
    assert (datetime.now(UTC).replace(tzinfo=None) - _updated_at(seeded_db)) < (
        timedelta(seconds=live_window)
    )


def test_the_reply_names_the_busy_run_and_walks_nothing(
    seeded_db, tmp_path, broker_active, live_window
):
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    key = backfill_runs.params_key(reprocess=True)
    run = backfill_runs.resolve("backfill_patch_series", key, start="resume").run
    get_active_writer().submit(
        WriteOp(label="test", fn=backfill_runs.advance(run.id, 5, 3))
    ).result(timeout=10)

    reply = dispatch(
        BackfillPatchSeriesRequest(rpc_id=1, reprocess=True, start="resume")
        .model_dump_json()
        .encode()
    )

    assert reply.ok is True, reply.error
    assert reply.result["partial"] is False
    assert reply.result["counters"] == {}
    info = reply.result["run"]
    assert (info["id"], info["cursor"], info["examined"]) == (run.id, 5, 3)
    assert info["busy"] is True
    assert 0 < info["retry_after"] <= live_window
    assert info["updated_at"]


def test_the_reply_names_the_refused_run_and_walks_nothing(
    seeded_db, tmp_path, broker_active, live_window
):
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    key = backfill_runs.params_key(reprocess=True)
    run = backfill_runs.resolve("backfill_patch_series", key, start="resume").run
    get_active_writer().submit(
        WriteOp(label="test", fn=backfill_runs.advance(run.id, 5, 3))
    ).result(timeout=10)
    # Not live, so the restart is refused for its progress, not as busy.
    _set_updated_at(seeded_db, 10 * live_window)

    reply = dispatch(
        BackfillPatchSeriesRequest(rpc_id=1, reprocess=True, start="restart")
        .model_dump_json()
        .encode()
    )

    assert reply.ok is True, reply.error
    assert reply.result["partial"] is False
    assert reply.result["counters"] == {}
    info = reply.result["run"]
    assert (info["id"], info["cursor"], info["examined"]) == (run.id, 5, 3)
    assert info["refused"] is True
    assert "busy" not in info and "retry_after" not in info
    assert info["updated_at"]


@pytest.mark.parametrize("flags", [(), ("--restart",), ("--restart", "--yes")])
def test_the_cli_says_the_run_is_in_progress_and_leaves_it(
    flags, seeded_db, tmp_path, broker_active, live_window
):
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    key = backfill_runs.params_key(reprocess=True)
    run = backfill_runs.resolve("backfill_patch_series", key, start="resume").run
    get_active_writer().submit(
        WriteOp(label="test", fn=backfill_runs.advance(run.id, 5, 3))
    ).result(timeout=10)
    before = _run_row(seeded_db), _updated_at(seeded_db)

    result = _invoke(backfill_patch_series_command, "--reprocess", *flags)

    assert result.exit_code != 0, result.output
    m = re.search(
        rf"run {run.id} is in progress \(last chunk (.+) UTC, at id 5\); "
        r"another session is driving it\. Stop it first, or wait (\d+) s "
        r"if it was just killed\.",
        result.output,
    )
    assert m is not None, result.output
    assert m.group(1) == before[1].isoformat(sep=" ", timespec="seconds")
    assert 0 < int(m.group(2)) <= live_window
    assert re.search(r"\bexamined=\d+", result.output) is None, "a chunk walked"
    assert (_run_row(seeded_db), _updated_at(seeded_db)) == before


def test_a_slow_writer_does_not_end_a_canonicals_chunk(
    seeded_db, tmp_path, broker_active, monkeypatch, caplog
):
    """The promotion sweep is queued behind other writes. Waiting for
    it timing out used to raise out of the walk; the sweep still runs
    when the writer reaches it, so the walk carries on and says so once."""
    from mimir import _pending_backfill

    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)

    class _Queued:
        def result(self, timeout=None):
            raise TimeoutError

    swept = {"n": 0}

    def slow_sweep(writer):
        swept["n"] += 1
        return _Queued()

    monkeypatch.setattr(
        _pending_backfill, "_submit_promote_list_address_sweep", slow_sweep
    )
    caplog.set_level("WARNING", logger=canonicals_backfill.logger.name)

    out = canonicals_backfill.backfill_canonicals(reprocess=True, promote_every=2)

    assert out.examined == _N and out.partial is False
    assert swept["n"] == _N // 2 + 1, "periodic sweeps and the final one"
    assert len([r for r in caplog.records if "sweep" in r.getMessage()]) == 1


def test_a_restart_refused_for_progress_does_not_claim_the_run(
    seeded_db, broker_active, live_window
):
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    key = backfill_runs.params_key(reprocess=True)
    run = backfill_runs.resolve("b", key, start="resume").run
    get_active_writer().submit(
        WriteOp(label="test", fn=backfill_runs.advance(run.id, 5, 3))
    ).result(timeout=10)
    _set_updated_at(seeded_db, 10 * live_window)
    before = _run_row(seeded_db), _updated_at(seeded_db)

    assert backfill_runs.resolve("b", key, start="restart").refused is not None
    assert (_run_row(seeded_db), _updated_at(seeded_db)) == before
