"""Shared fixtures for broker server/client tests: spin up the
broker in a thread on a tmp UNIX socket, run a body, tear down."""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from mimir.broker import _context
from mimir.broker.server import build_server


def short_socket_path(name: str) -> Path:
    """Build a short socket path under `/tmp`. macOS' default
    `tmp_path` lives under `/private/var/folders/...` which busts
    the AF_UNIX 104-byte limit; we sidestep by rooting in `/tmp`
    with a pid-tagged filename short enough to fit. Caller is
    responsible for cleanup if `broker_running` isn't used."""
    return Path("/tmp") / f"mimir-test-{os.getpid()}-{name}.sock"


@contextmanager
def broker_running(socket_path: Path):
    """Start a broker on `socket_path` in a background thread, yield
    the server instance back, shut it down cleanly on exit. Use as
    `with broker_running(short_socket_path('foo')) as server: ...`.
    Tests must not call `mimir.broker.server.serve()` directly
    because signal handler registration only works in the main
    thread.

    Saves and restores the module-level active pool + writer so the
    session-scoped _session_broker fixture's registration survives.
    Without the restore, every test that uses broker_running would
    leave _context cleared, causing subsequent tests that rely on
    the session broker (e.g. test_cli/test_cache.py warm-cache tests)
    to fail with "No active broker; call set_active() first"."""
    saved_pool = _context._active_pool
    saved_writer = _context._active_writer
    server = build_server(socket_path)
    server.writer.start()
    # Phase 2 of the two-pool restructure: register active context
    # so handlers can reach read_pool and writer without dispatcher refactor.
    _context.set_active(server.read_pool, server.writer)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
        name="test-broker",
    )
    thread.start()
    try:
        yield server
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server.writer.stop(timeout=10.0)
        server.read_pool.close()
        sp = Path(socket_path)
        if sp.exists():
            sp.unlink()
        # Restore the session broker's registration so subsequent
        # tests can still reach the active pool.
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)
        else:
            _context.clear_active()
