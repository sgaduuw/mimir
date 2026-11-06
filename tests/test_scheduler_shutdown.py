"""Exercise scheduler stop signals through real shell processes and children."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

SCHEDULER = Path(__file__).resolve().parent.parent / "deploy" / "scheduler.sh"
SHELLS = [shutil.which("sh")]
if dash := shutil.which("dash"):
    if Path(dash).resolve() != Path(SHELLS[0]).resolve():
        SHELLS.append(dash)

STUB = """import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
kind='sleep' if Path(sys.argv[0]).name=='sleep' else ' '.join(args)
with open(os.environ['SCHEDULER_TEST_EVENTS'],'a') as f:
    f.write(json.dumps({'kind':kind,'pid':os.getpid(),'ppid':os.getppid()})+'\\n')
if kind=='sleep':
    time.sleep(float(os.environ.get('SCHEDULER_TEST_TICK','60')))
elif kind==os.environ.get('SCHEDULER_TEST_HOLD'):
    release=os.environ.get('SCHEDULER_TEST_RELEASE')
    if release:
        while not Path(release).exists(): time.sleep(.01)
    else:
        time.sleep(60)
"""


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _wait_for_event(path, kind, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        matches = [event for event in _events(path) if event["kind"] == kind]
        if matches:
            return matches[-1]
        assert process.poll() is None, "scheduler exited before the requested phase"
        time.sleep(0.01)
    pytest.fail(f"scheduler never entered {kind}")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@contextmanager
def _scheduler(tmp_path, shell, phase, extra_env=None):
    data = tmp_path / "data"
    data.mkdir()
    sentinel_names = [
        "warm_fast",
        "warm_slow",
        "update",
        "update_mainline",
        "analyze_full",
        "vacuum",
    ]
    for name in sentinel_names:
        (data / f".last_{name}").touch()
    held = {
        "foreground": "update",
        "initial": "warm-cache --tier fast",
        "slow": "warm-cache --tier slow",
    }.get(phase, "")
    if phase == "paused":
        (data / ".scheduler-paused").touch()
    if phase in {"foreground", "slow"}:
        target = data / (".last_update" if phase == "foreground" else ".last_warm_slow")
        os.utime(target, (1, 1))
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in ("mimir", "sleep"):
        stub = stub_dir / name
        stub.write_text(f"#!{sys.executable}\n{STUB}")
        stub.chmod(0o755)
    script = tmp_path / "scheduler.sh"
    script.write_text(SCHEDULER.read_text().replace("/data", str(data)))
    event_path = tmp_path / "events.jsonl"
    env = dict(os.environ, PATH=str(stub_dir) + os.pathsep + os.environ["PATH"])
    for name in [
        "WARM_CACHE_EVERY",
        "WARM_CACHE_SLOW_EVERY",
        "UPDATE_EVERY",
        "UPDATE_MAINLINE_EVERY",
        "ANALYZE_FULL_EVERY",
        "VACUUM_EVERY",
    ]:
        env[name] = "100000000"
    env.update(SCHEDULER_TEST_EVENTS=str(event_path), SCHEDULER_TEST_HOLD=held)
    env.update(extra_env or {})
    log = tmp_path / "output.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [shell, str(script)],
            env=env,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
        try:
            yield process, event_path, data, log, held
        finally:
            # Isolated session cleanup also runs against the unfixed script.
            if process.poll() is None or any(
                _alive(event["pid"]) for event in _events(event_path)
            ):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)


@pytest.mark.parametrize("shell", SHELLS, ids=lambda p: Path(p).name)
@pytest.mark.parametrize(
    "stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["TERM", "INT"]
)
@pytest.mark.parametrize("phase", ["idle", "paused", "foreground", "initial", "slow"])
def test_scheduler_stops_cleanly_with_its_children(tmp_path, shell, stop_signal, phase):
    with _scheduler(tmp_path, shell, phase) as (process, event_path, data, log, held):
        before = {path.name: path.stat().st_mtime_ns for path in data.glob(".last_*")}
        wanted = held or "sleep"
        child = _wait_for_event(event_path, wanted, process)
        assert _alive(child["pid"]), "the phase's command must be running"
        if phase in {"initial", "slow"}:
            _wait_for_event(event_path, "sleep", process)
        owned = {
            pid
            for event in _events(event_path)
            for pid in (event["pid"], event["ppid"])
        }
        owned.discard(process.pid)
        started = time.monotonic()
        process.send_signal(stop_signal)
        process.wait(timeout=2)
        assert process.returncode == 0, log.read_text()
        assert time.monotonic() - started < 2
        deadline = time.monotonic() + 2
        while any(_alive(pid) for pid in owned) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(_alive(pid) for pid in owned), (
            "scheduler left an owned child alive"
        )
        after_events = _events(event_path)
        assert not any(
            event["kind"] in {"update-mainline", "analyze", "vacuum"}
            for event in after_events
        )
        if phase in {"foreground", "initial", "slow"}:
            sentinel = {
                "foreground": "update",
                "initial": "warm_fast",
                "slow": "warm_slow",
            }[phase]
            assert (data / f".last_{sentinel}").stat().st_mtime_ns == before[
                f".last_{sentinel}"
            ]


@pytest.mark.parametrize("shell", SHELLS, ids=lambda p: Path(p).name)
def test_signal_between_spawn_and_pid_capture_still_stops_child(tmp_path, shell):
    from tests.test_scheduler_sentinels import _extract_multiline_function

    source = SCHEDULER.read_text()
    definitions = ["log() { :; }"]
    for name in ["stop", "start_command", "wait_command"]:
        if f"\n{name}() {{" in source:
            definitions.append(_extract_multiline_function(name, source))
    pidfile = tmp_path / "child.pid"
    # Inject the signal into the actual launch helper before its PID capture.
    # A temporary deferred trap must keep this precise gap from orphaning it.
    script = "\n".join(definitions).replace(
        "command_pid=$!",
        f'while [ ! -f "{pidfile}" ]; do :; done\n    kill -TERM $$\n    command_pid=$!',
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import os,time\nfrom pathlib import Path\n"
        f"pidfile = Path({str(pidfile)!r})\n"
        # Publish readiness only after the PID is fully written, before signaling.
        "pending = pidfile.with_suffix('.pending')\n"
        "pending.write_text(str(os.getpid()))\npending.replace(pidfile)\n"
        "time.sleep(60)\n"
    )
    command = tmp_path / "launch.sh"
    command.write_text(
        script + f'\ntrap stop TERM INT\nwait_command "{sys.executable}" "{child}"\n'
    )
    process = subprocess.Popen([shell, str(command)], start_new_session=True)
    try:
        assert process.wait(timeout=3) == 0
        assert pidfile.exists(), "the injected race must run after the child starts"
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _alive(pid), "stop missed the child between spawn and PID capture"
    finally:
        # This session belongs to the test, including any child missed on failure.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


@pytest.mark.parametrize("shell", SHELLS, ids=lambda p: Path(p).name)
def test_slow_background_completion_allows_next_cycle_without_overlap(tmp_path, shell):
    release = tmp_path / "release-slow"
    env = {
        "SCHEDULER_TEST_TICK": "0.03",
        "SCHEDULER_TEST_RELEASE": str(release),
        "WARM_CACHE_SLOW_EVERY": "1",
    }
    with _scheduler(tmp_path, shell, "slow", env) as (process, events, data, log, _):
        first = _wait_for_event(events, "warm-cache --tier slow", process)
        assert _alive(first["pid"])
        # Let more than a full cadence pass while the first slow job is held.
        time.sleep(1.2)
        assert len([e for e in _events(events) if e["kind"] == first["kind"]]) == 1
        assert (data / ".last_warm_slow").stat().st_mtime == 1
        release.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cycles = [e for e in _events(events) if e["kind"] == first["kind"]]
            if len(cycles) >= 2 and (data / ".last_warm_slow").stat().st_mtime > 1:
                break
            assert process.poll() is None, log.read_text()
            time.sleep(0.01)
        assert len(cycles) >= 2, "completed background job never became eligible again"
        assert not _alive(first["pid"]), (
            "next cycle began before the old command exited"
        )
        assert (data / ".last_warm_slow").stat().st_mtime > 1
        process.terminate()
        assert process.wait(timeout=2) == 0, log.read_text()
