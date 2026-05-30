"""WriteOp / WriteFuture / WriterThread tests, Phase 1 of the
two-pool restructure."""

import dataclasses

from mimir.broker.writes import WriteOp


def test_write_op_is_frozen():
    op = WriteOp(label="test", fn=lambda c: None)
    assert dataclasses.is_dataclass(op)
    # frozen=True so reassigning the label fails
    try:
        op.label = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("WriteOp should be frozen")


def test_write_op_holds_label_and_fn():
    sentinel = object()

    def fn(conn):
        return sentinel

    op = WriteOp(label="cache.set:k", fn=fn)
    assert op.label == "cache.set:k"
    assert op.fn is fn
