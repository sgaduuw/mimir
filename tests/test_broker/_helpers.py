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
    thread."""
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
        _context.clear_active()
        server.read_pool.close()
        sp = Path(socket_path)
        if sp.exists():
            sp.unlink()
