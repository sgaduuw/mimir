"""Shared fixtures for broker server/client tests: spin up the
broker in a thread on a tmp UNIX socket, run a body, tear down."""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

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
    the socket path back, shut it down cleanly on exit. Use as
    `with broker_running(short_socket_path('foo')) as sp: ...`.
    Tests must not call `mimir.broker.server.serve()` directly
    because signal handler registration only works in the main
    thread."""
    server = build_server(socket_path)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05},
        daemon=True, name="test-broker",
    )
    thread.start()
    try:
        yield Path(socket_path)
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        sp = Path(socket_path)
        if sp.exists():
            sp.unlink()
