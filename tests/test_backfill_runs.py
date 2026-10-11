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

import pytest
from click.testing import CliRunner
from sqlalchemy import delete, select, text, update

from mimir import _pending_backfill
from mimir._backfill import walk_articles
from mimir.broker.handlers import dispatch
from mimir.broker.protocol import BackfillPatchSeriesRequest
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
        BackfillPatchSeriesRequest(rpc_id=1, reprocess=True).model_dump_json().encode()
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

    restarted = _invoke(backfill_patch_series_command, "--reprocess", "--restart")
    assert restarted.exit_code == 0, restarted.output
    assert _resumed_at(restarted.output) is None
    assert _examined(restarted.output) == _N
    # The discarded run is gone; the new one walked to the oldest id.
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


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
    assert re.search(r"\b7 examined in total\b", resumed.output), resumed.output
    assert [r[2:] for r in _runs(seeded_db)] == [(ids[-1], _N, True)]


def test_advance_never_moves_the_cursor_back_up(seeded_db, broker_active):
    """Two overlapping chunks of one run (possible with more than one
    long worker) commit in either order; the cursor keeps the lower."""
    from mimir import backfill_runs
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    run = backfill_runs.resolve("b", backfill_runs.params_key(reprocess=True))
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
