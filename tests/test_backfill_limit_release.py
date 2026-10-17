"""`--limit` caps one session only; the next invocation continues the
run (docs/archive.md). A session that ends on its limit releases its
claim, so that rerun is not refused as another session driving the run.

Unlike tests/test_backfill_runs.py this module runs with the real
`LIVE_RUN_SEC` window, which is the whole point.
"""

import pytest

from mimir.config import settings
from tests.test_backfill_runs import (
    BACKFILLS,
    _corpus,
    _examined,
    _invoke,
    _small_batches,
)


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_next_invocation_continues_a_limited_session(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    command, _submit = BACKFILLS[name]
    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)

    first = _invoke(command, "--reprocess", "--limit", "2")
    assert first.exit_code == 0, first.output
    assert _examined(first.output) == 2

    second = _invoke(command, "--reprocess", "--limit", "2")
    assert second.exit_code == 0, second.output
    assert _examined(second.output) == 2


@pytest.mark.parametrize("name", list(BACKFILLS))
def test_a_time_budget_that_ends_on_the_limit_releases_too(
    name, seeded_db, tmp_path, broker_active, monkeypatch
):
    """The chunk comes back partial, but the CLI has no budget left and
    stops, so the claim must be released the same way."""
    command, _submit = BACKFILLS[name]
    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)

    first = _invoke(command, "--reprocess", "--limit", "2")
    assert first.exit_code == 0, first.output
    assert _examined(first.output) == 2

    second = _invoke(command, "--reprocess", "--limit", "2")
    assert second.exit_code == 0, second.output
    assert _examined(second.output) == 2


def test_a_session_that_is_not_done_keeps_its_claim(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """Below the limit the CLI carries on, so the run stays claimed and
    a second session is still refused."""
    command, _submit = BACKFILLS["patch_series"]
    _corpus(seeded_db, tmp_path)
    _small_batches(monkeypatch)
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)

    first = _invoke(command, "--reprocess", "--limit", "4")
    assert first.exit_code == 0, first.output
    assert _examined(first.output) == 4  # two chunks, released after the last

    # A session that stops short of its limit (killed) is not released.
    from mimir.broker.handlers import dispatch
    from mimir.broker.protocol import BackfillPatchSeriesRequest

    reply = dispatch(
        BackfillPatchSeriesRequest(rpc_id=1, reprocess=True, limit=4, start="resume")
        .model_dump_json()
        .encode()
    )
    # Precondition: the setup dispatch was let in, so a failure below is
    # about the claim, not about setup.
    assert not reply.result["run"].get("busy")
    assert reply.result["counters"]["examined"] == 2 < 4
    refused = _invoke(command, "--reprocess")
    assert refused.exit_code != 0
    assert "is in progress" in refused.output
