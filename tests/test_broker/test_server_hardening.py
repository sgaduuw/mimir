"""Hardening pins for the broker daemon: socket-file mode at bind
time (not just after the trailing chmod), per-connection line-length
cap, idle-timeout, peer-credential check on Linux, and the wire-
boundary slug validator on `cache_delete_for_inbox`. Each test pins
one defense; together they cover the Phase A hardening commit."""

import logging
import os
import socket
import time

import pytest
from pydantic import ValidationError

from mimir.broker import server as broker_server
from mimir.broker.protocol import CacheDeleteForInboxRequest
from mimir.broker.server import build_server
from tests.test_broker._helpers import broker_running, short_socket_path


def test_socket_node_is_born_0660_even_with_lax_umask(seeded_db):
    """The umask dance in `build_server` forces the socket node to be
    born `0660`. Pinning the mode after a lax-umask process state
    catches a regression that loses the umask save/restore and falls
    back to relying on the trailing `os.chmod` alone, which races a
    local connector that wins between bind() and chmod()."""
    prev_umask = os.umask(0o022)
    try:
        sp = short_socket_path("born-0660")
        server = build_server(sp)
        try:
            mode = sp.stat().st_mode & 0o777
            assert mode == 0o660, f"expected 0o660, got {oct(mode)}"
        finally:
            server.server_close()
            if sp.exists():
                sp.unlink()
    finally:
        os.umask(prev_umask)


def test_reader_closes_connection_on_oversized_line(seeded_db, caplog, monkeypatch):
    """A peer that pushes more than `MAX_REQUEST_BYTES` without a
    newline is dropped. Without the cap the reader's `linebuf` would
    grow until the broker OOMs. Test compresses the cap to 8 KiB so
    the round-trip stays fast; the production cap (16 MiB) is
    exercised end-to-end by the large-payload sitemap test."""
    caplog.set_level(logging.WARNING, logger="mimir.broker.server")
    monkeypatch.setattr(broker_server, "MAX_REQUEST_BYTES", 8 * 1024)

    sp = short_socket_path("oversized")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        try:
            # Push more than the (test-only) cap. The server should
            # close on us; we may see the close mid-send (BrokenPipe)
            # or after the full payload (empty read).
            payload = b"a" * (12 * 1024)
            try:
                s.sendall(payload)
            except BrokenPipeError, ConnectionResetError:
                pass
            s.settimeout(2.0)
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
            except TimeoutError, ConnectionResetError, OSError:
                pass
        finally:
            s.close()
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("exceeded" in m and "newline" in m for m in msgs), (
        f"expected an oversized-line warning; saw {msgs}"
    )


def test_normal_sized_request_passes(seeded_db):
    """Sanity check that the cap doesn't break healthy traffic.
    A ping is a few bytes."""
    sp = short_socket_path("normal")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        try:
            s.sendall(b'{"rpc_id":1,"op":"ping"}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            assert b'"ok":true' in buf
        finally:
            s.close()


def test_reader_closes_idle_connection(seeded_db, caplog, monkeypatch):
    """A connection that accepts but never speaks gets closed after
    `IDLE_TIMEOUT_SEC`. Test compresses the timeout via monkeypatch
    so it actually fires inside the test budget."""
    caplog.set_level(logging.WARNING, logger="mimir.broker.server")
    # Tight idle window for the test. Two select polls (each at
    # SHUTDOWN_POLL_SEC=100ms) easily cover the 500ms window.
    monkeypatch.setattr(broker_server, "IDLE_TIMEOUT_SEC", 0.5)

    sp = short_socket_path("idle")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        try:
            # Send nothing; wait past the idle ceiling + selector
            # poll, then verify the broker closed us out.
            time.sleep(1.0)
            # An EOF read after close returns empty bytes promptly.
            s.settimeout(2.0)
            data = s.recv(4096)
            assert data == b"", "expected broker-side close on idle"
        finally:
            s.close()

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("idle connection" in m for m in msgs), (
        f"expected idle-close warning; saw {msgs}"
    )


def test_active_connection_not_closed_by_idle_check(seeded_db, monkeypatch):
    """A connection that pings periodically inside the idle window
    stays open. Catches a regression that would close a healthy
    persistent client because `last_activity` wasn't updated on
    successful reads."""
    monkeypatch.setattr(broker_server, "IDLE_TIMEOUT_SEC", 0.5)

    sp = short_socket_path("active")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        rfile = s.makefile("rb")
        wfile = s.makefile("wb")
        try:
            # Three pings inside what would otherwise be three full
            # idle windows.
            for _ in range(3):
                wfile.write(b'{"rpc_id":1,"op":"ping"}\n')
                wfile.flush()
                line = rfile.readline()
                assert b'"ok":true' in line
                time.sleep(0.2)
            # If the connection had been closed mid-loop, the next
            # readline would return empty.
        finally:
            rfile.close()
            wfile.close()
            s.close()


@pytest.mark.skipif(
    not broker_server._HAS_SO_PEERCRED,
    reason="SO_PEERCRED not available on this platform (macOS/BSD)",
)
def test_peer_uid_mismatch_is_rejected(seeded_db, caplog, monkeypatch):
    """On Linux the broker reads SO_PEERCRED and refuses any peer
    whose euid doesn't match its own. Faking the expected euid to an
    unreachable value makes every test-side connect look like a
    mismatch; the broker should log + close without enqueuing work."""
    caplog.set_level(logging.WARNING, logger="mimir.broker.server")
    # Capture the original euid, then pretend the broker expects a
    # different one. Any UID that we definitely don't have works; -1
    # never matches a real euid.
    monkeypatch.setattr(broker_server, "_BROKER_EUID", -1)

    sp = short_socket_path("peer-mismatch")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        try:
            # Broker close manifests as an empty read.
            s.settimeout(2.0)
            try:
                data = s.recv(4096)
            except ConnectionResetError, OSError:
                data = b""
            assert data == b"", "expected close on rejected peer"
        finally:
            s.close()

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("refusing connection" in m for m in msgs), (
        f"expected peer-mismatch warning; saw {msgs}"
    )


def test_check_peer_uid_skips_when_so_peercred_absent(monkeypatch):
    """On macOS / BSDs the symbol is absent; the check short-circuits
    True so the platform isn't blocked from running broker tests."""
    monkeypatch.setattr(broker_server, "_HAS_SO_PEERCRED", False)
    # A real-but-unrelated socket; the function should never touch it
    # under the platform-skip branch.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert broker_server._check_peer_uid(s) is True
    finally:
        s.close()


def test_cache_delete_for_inbox_rejects_like_metacharacters():
    """Slug regex on the wire boundary refuses names containing LIKE
    metacharacters (`%`, `_`) and anything outside the
    `[a-z0-9-]` shape. The CLI write paths already slug-validate
    before the call ever reaches cache; the broker-side validator is
    defense-in-depth against a buggy or hostile peer."""
    bad = ["%lkml%", "lkml_", "_lkml", "LKML", "lkml.dev", "-lkml", "lkml-"]
    for name in bad:
        with pytest.raises(ValidationError):
            CacheDeleteForInboxRequest(rpc_id=1, name=name)


def test_cache_delete_for_inbox_accepts_real_slugs():
    """Sanity check: every shape that `mimir.inboxes.validate_name`
    accepts must round-trip through the broker validator. Keeps the
    two regexes drift-aware."""
    good = ["lkml", "linux-fsdevel", "x86", "a", "a1", "a-b-c", "linux-arm-kernel"]
    for name in good:
        req = CacheDeleteForInboxRequest(rpc_id=1, name=name)
        assert req.name == name
