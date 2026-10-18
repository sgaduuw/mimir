"""The fast healthcheck ping, `mimir_broker_ping` (#758).

It speaks the broker's wire protocol without going through the
`mimir` package, so the real-broker test below is also the
emitter/acceptor pin: if `PingRequest` or `dispatch()` changes shape,
the healthcheck's hand-built bytes stop being accepted here, not in
production.
"""

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

import mimir_broker_ping
from mimir.cli.broker import broker_ping_command
from tests.test_broker._helpers import broker_running, short_socket_path


def test_ping_against_real_broker():
    sock = short_socket_path("light-ping")
    with broker_running(sock):
        mimir_broker_ping.ping(sock)
        assert mimir_broker_ping.main(["--socket", str(sock)]) == 0
        result = CliRunner().invoke(broker_ping_command, ["--socket", str(sock)])
    assert result.exit_code == 0, result.output
    assert result.output == "ok\n"


def test_missing_socket_fails(capsys):
    sock = short_socket_path("light-ping-missing")
    assert not sock.exists()
    with pytest.raises(OSError):
        mimir_broker_ping.ping(sock)
    assert mimir_broker_ping.main(["--socket", str(sock)]) == 1
    assert capsys.readouterr().err.startswith("broker ping failed: ")
    result = CliRunner().invoke(broker_ping_command, ["--socket", str(sock)])
    assert result.exit_code == 1


def _serve_one_reply(path: Path, reply: bytes) -> threading.Thread:
    """Answer one connection with `reply` after reading its request."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def run():
        with srv:
            conn, _ = srv.accept()
            with conn:
                conn.makefile("rb").readline()
                conn.sendall(reply)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    "reply",
    [
        b'{"rpc_id":1,"ok":false,"error":"InvalidRequest"}\n',
        b'{"rpc_id":2,"ok":true}\n',
        b"not json\n",
        b"",
    ],
    ids=["not-ok", "wrong-rpc-id", "garbage", "eof"],
)
def test_bad_reply_fails(reply):
    sock = short_socket_path("light-ping-fake")
    thread = _serve_one_reply(sock, reply)
    try:
        with pytest.raises(ValueError):
            mimir_broker_ping.ping(sock)
    finally:
        thread.join(timeout=5)
        sock.unlink()


def test_import_stays_light():
    """Importing the module in a fresh interpreter must not pull in the
    app. `-I` drops cwd and PYTHON* env, so the module is found through
    the installed project, as the console script finds it."""
    code = (
        "import sys, mimir_broker_ping; "
        "heavy = ('flask', 'sqlalchemy', 'dulwich', 'pydantic', 'mimir'); "
        "print(','.join(m for m in heavy if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip() == ""
