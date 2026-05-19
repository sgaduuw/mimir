"""Tests for `mimir.broker.server`'s lifecycle: socket binding,
accept loop, clean shutdown, stale-socket cleanup. Uses an
in-thread broker on a tmp UNIX socket. RPC content is tested
in `test_handlers.py`; here we care about the socket lifecycle
and the bytes-on-the-wire framing."""

import socket
from pathlib import Path

from mimir.broker.server import build_server
from tests.test_broker._helpers import broker_running, short_socket_path


def _send_line(sp: Path, line: bytes) -> bytes:
    """One-shot connect-send-recv-close, for tests that don't
    exercise the persistent-connection path (that's `test_client`)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(str(sp))
    s.sendall(line + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return buf


def test_server_binds_and_responds_to_ping(seeded_db):
    sp = short_socket_path("ping")
    with broker_running(sp):
        reply = _send_line(sp, b'{"op": "ping"}')
        assert b'"ok":true' in reply


def test_server_handles_sequential_requests_on_one_connection(seeded_db):
    """A single client connection sends N requests serially; broker
    reads each line and replies in order. Confirms the broker's
    StreamRequestHandler loops, not single-request-then-close."""
    sp = short_socket_path("seq")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        rfile = s.makefile("rb")
        wfile = s.makefile("wb")
        try:
            for _ in range(5):
                wfile.write(b'{"op": "ping"}\n')
                wfile.flush()
                line = rfile.readline()
                assert b'"ok":true' in line
        finally:
            rfile.close()
            wfile.close()
            s.close()


def test_server_unlinks_stale_socket_on_startup(seeded_db):
    """A stale socket file from an unclean prior shutdown must not
    prevent the new broker from binding. `build_server` unlinks it
    before bind()."""
    sp = short_socket_path("stale")
    # Pre-create the file as a stale socket-shaped file. (A real
    # AF_UNIX-listened socket would need a previous process; a
    # plain file is enough to exercise the unlink-first path.)
    sp.write_bytes(b"")
    assert sp.exists()

    server = build_server(sp)
    try:
        assert sp.is_socket()
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_server_socket_file_has_mode_0660(seeded_db):
    """The broker chmods the socket to 0660 so only the mimir UID
    (and its group) can talk to it. Trust model: same as the /data
    volume's file perms; no extra auth on RPC."""
    sp = short_socket_path("mode")
    server = build_server(sp)
    try:
        mode = sp.stat().st_mode & 0o777
        assert mode == 0o660, f"expected 0o660, got {oct(mode)}"
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_server_cleans_up_socket_file_on_shutdown(seeded_db):
    """After the broker shuts down (in production: SIGTERM; in
    tests: stop_event + server_close + manual unlink in the helper),
    the socket file must not be left behind to confuse the next
    boot. `broker_running` does the unlink; verify it succeeded."""
    sp = short_socket_path("cleanup")
    with broker_running(sp):
        assert sp.exists()
    assert not sp.exists()
