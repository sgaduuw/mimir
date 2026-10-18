"""`mimir-broker-ping`: the broker healthcheck, stdlib only.

It lives outside the `mimir` package on purpose (#758). Importing any
`mimir.*` module runs `mimir/__init__.py`, which loads Flask,
SQLAlchemy, dulwich, pydantic and every CLI and web module. Under CPU
contention that import alone outlasted the healthcheck timeout, so the
broker was reported unhealthy while it answered every ping.

The request below is the wire form of `PingRequest` with `rpc_id` 1.
`tests/test_broker/test_broker_ping.py` sends it to a real broker, so a
protocol change fails there rather than in production.
"""

import argparse
import json
import socket
import sys

RPC_ID = 1
REQUEST = b'{"rpc_id":1,"op":"ping"}\n'


def ping(socket_path, timeout: float = 5.0) -> None:
    """Return if the broker answers `ok` to a ping. Raise OSError when
    the socket fails or times out, ValueError on any other reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(REQUEST)
        # A ping reply is a few dozen bytes; the cap stops a confused
        # peer from streaming into memory.
        line = sock.makefile("rb").readline(65536)
    reply = json.loads(line)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise ValueError(f"reply not ok: {line!r}")
    if reply.get("rpc_id") != RPC_ID:
        raise ValueError(f"reply for another request: {line!r}")


def main(argv: list[str] | None = None) -> int:
    """Print `ok` and return 0, or print the reason to stderr and
    return 1. `mimir broker-ping` delegates here too."""
    parser = argparse.ArgumentParser(
        prog="mimir-broker-ping",
        description="Probe a running mimir broker. Exits 0 on success, 1 otherwise.",
    )
    parser.add_argument(
        "--socket", required=True, help="UNIX socket path of a running broker."
    )
    args = parser.parse_args(argv)
    try:
        ping(args.socket)
    except (OSError, ValueError) as exc:
        print(f"broker ping failed: {args.socket}: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0
