"""Host sharing uses the configured directory, not a container's PID scope."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.config import Settings
from mimir.web import crawler_budget

BOOT = "12345678-1234-4567-8123-123456789abc"


@pytest.fixture
def host_metadata(monkeypatch, tmp_path):
    real_stat = os.stat
    real_read = Path.read_text
    metadata = {
        "boot": BOOT,
        "offsets": "monotonic 0 0\nboottime 0 0\n",
        "pid": 100,
        "parent": 200,
        "time": 300,
        "time_for_children": 300,
    }

    def stat(path, *args, **kwargs):
        name = str(path).removeprefix("/proc/self/ns/")
        if name in ("pid", "time", "time_for_children"):
            return SimpleNamespace(st_dev=4, st_ino=metadata[name])
        return real_stat(path, *args, **kwargs)

    def read(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return metadata["boot"]
        if str(path) == "/proc/self/timens_offsets":
            return metadata["offsets"]
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "getppid", lambda: metadata["parent"])
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(crawler_budget, "shared_dir", lambda: str(tmp_path))
    return metadata


def settings(**kwargs):
    return Settings(_env_file=None, crawler_budget_per_minute=120, WORKERS=8, **kwargs)


def test_host_setting_uses_env_and_is_unset_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CRAWLER_BUDGET_SHARED_DIR", raising=False)
    assert Settings(_env_file=None).crawler_budget_shared_dir is None
    monkeypatch.setenv("CRAWLER_BUDGET_SHARED_DIR", str(tmp_path))
    assert Settings(_env_file=None).crawler_budget_shared_dir == str(tmp_path)


def test_host_file_ignores_pid_scope_and_workers(tmp_path, host_metadata):
    configured = settings(crawler_budget_shared_dir=str(tmp_path))
    first = crawler_budget.CrawlerBudget.from_settings(configured)
    host_metadata.update(pid=101, parent=201)
    second = crawler_budget.CrawlerBudget.from_settings(
        configured.model_copy(update={"web_workers": 3})
    )
    assert first._shared._fd is not None
    assert second._shared._fd is not None
    assert os.fstat(first._shared._fd).st_ino == os.fstat(second._shared._fd).st_ino
    assert len(list(tmp_path.iterdir())) == 1
    assert BOOT in next(tmp_path.iterdir()).name


def test_default_keeps_pid_namespace_and_parent_isolation(tmp_path, host_metadata):
    for change in ({}, {"pid": 101}, {"parent": 201}):
        host_metadata.update(change)
        gate = crawler_budget.CrawlerBudget.from_settings(settings())
        assert gate._shared._fd is not None
    assert len(list(tmp_path.iterdir())) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"boot": "bad/boot"},
        {"time_for_children": 301},
        {"offsets": "monotonic 1 0\nboottime 0 0\n"},
        {"offsets": "monotonic 0 1\nboottime 0 0\n"},
        {"offsets": "malformed"},
    ],
)
def test_unverified_host_clock_falls_back(tmp_path, host_metadata, caplog, change):
    host_metadata.update(change)
    gate = crawler_budget.CrawlerBudget.from_settings(
        settings(crawler_budget_shared_dir=str(tmp_path))
    )
    assert gate._shared._fd is None
    assert gate._global.rate == pytest.approx(120 / 8 / 60)
    assert "cannot share buckets" in caplog.text
    assert not list(tmp_path.iterdir())


def test_existing_wrong_size_is_not_truncated(tmp_path):
    path = tmp_path / "budget"
    original = b"existing state must survive"
    path.write_bytes(original)
    with pytest.raises(OSError, match="size"):
        crawler_budget.CrawlerBudget(
            120, {"testbot": 100}, 1, 120, shared_path=str(path)
        )
    assert path.read_bytes() == original


def test_boot_and_layout_get_new_files(tmp_path, host_metadata):
    configured = settings(crawler_budget_shared_dir=str(tmp_path))
    crawler_budget.CrawlerBudget.from_settings(configured)
    host_metadata["boot"] = "22345678-1234-4567-8123-123456789abc"
    crawler_budget.CrawlerBudget.from_settings(configured)
    crawler_budget.CrawlerBudget.from_settings(
        configured.model_copy(update={"crawler_default_ua_pct": 11})
    )
    assert len(list(tmp_path.iterdir())) == 3


@pytest.mark.parametrize(
    "metadata_path",
    [
        "/proc/self/ns/time",
        "/proc/self/ns/time_for_children",
        "/proc/self/timens_offsets",
        "/proc/sys/kernel/random/boot_id",
    ],
)
def test_missing_metadata_falls_back(
    tmp_path, host_metadata, monkeypatch, caplog, metadata_path
):
    real_stat, real_read = os.stat, Path.read_text

    def stat(path, *args, **kwargs):
        if str(path) == metadata_path:
            raise FileNotFoundError(metadata_path)
        return real_stat(path, *args, **kwargs)

    def read(path, *args, **kwargs):
        if str(path) == metadata_path:
            raise PermissionError(metadata_path)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read)
    gate = crawler_budget.CrawlerBudget.from_settings(
        settings(crawler_budget_shared_dir=str(tmp_path))
    )
    assert gate._shared._fd is None
    assert gate._global.rate == pytest.approx(120 / 8 / 60)
    assert "cannot share buckets" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "failure",
    ["missing", "not-directory", "permissions", "relative", "symlink", "wrong-size"],
)
def test_bad_host_storage_falls_back_without_mutation(
    tmp_path, host_metadata, monkeypatch, caplog, failure
):
    directory = tmp_path / "shared"
    directory.mkdir()
    configured = settings(crawler_budget_shared_dir=str(directory))
    original = b"existing state must survive"
    target = None
    if failure == "missing":
        directory.rmdir()
    elif failure == "not-directory":
        directory.rmdir()
        directory.write_bytes(original)
        target = directory
    elif failure == "relative":
        configured = configured.model_copy(
            update={"crawler_budget_shared_dir": "relative"}
        )
    elif failure == "permissions":
        real_open = os.open

        def denied(path, *args, **kwargs):
            if Path(path).parent == directory:
                raise PermissionError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    else:
        # Discover the real name through the production path selector, then
        # close it before replacing it. No running gate ever loses its inode.
        gate = crawler_budget.CrawlerBudget.from_settings(configured)
        assert gate._shared._fd is not None
        gate._shared.mem.close()
        os.close(gate._shared._fd)
        path = next(directory.iterdir())
        path.unlink()
        target = tmp_path / "target"
        target.write_bytes(original)
        if failure == "symlink":
            path.symlink_to(target)
        else:
            path.write_bytes(original)
            target = path
    gate = crawler_budget.CrawlerBudget.from_settings(configured)
    assert gate._shared._fd is None
    assert gate._global.rate == pytest.approx(120 / 8 / 60)
    assert "cannot share buckets" in caplog.text
    if target:
        assert target.read_bytes() == original


def test_disabled_and_default_modes_do_not_read_host_metadata(tmp_path, monkeypatch):
    def unexpected():
        pytest.fail("host metadata read without opt-in and enabled budget")

    monkeypatch.setattr(crawler_budget, "_host_boot_id", unexpected)
    monkeypatch.setattr(crawler_budget, "shared_dir", lambda: str(tmp_path))
    crawler_budget.CrawlerBudget.from_settings(
        Settings(_env_file=None, crawler_budget_shared_dir=str(tmp_path))
    )
    crawler_budget.CrawlerBudget.from_settings(settings())


class FrozenBudget(crawler_budget.CrawlerBudget):
    """Inject only the supported clock; keep the production factory/storage."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, clock=lambda: 1000.0, **kwargs)


def _host_process(directory, result, barrier=None):
    from unittest.mock import patch

    real_open = os.open

    def cold_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if barrier is not None and Path(path).parent == Path(directory):
            # All constructors have opened the same initially empty file
            # before any can size or initialize it.
            barrier.wait(timeout=30)
        return fd

    with (
        patch.object(crawler_budget, "_host_boot_id", return_value=BOOT),
        patch.object(
            crawler_budget, "shared_dir", return_value=directory + "/unused-default"
        ),
        patch.object(os, "open", cold_open),
    ):
        gate = FrozenBudget.from_settings(
            Settings(
                _env_file=None,
                crawler_budget_shared_dir=directory,
                crawler_budget_per_minute=60,
                crawler_ua_budgets={"Googlebot": 100},
                WORKERS=8,
            )
        )
    assert gate._shared._fd is not None
    info = os.fstat(gate._shared._fd)
    admitted = sum(gate.check("Googlebot") is None for _ in range(100))
    result.put((os.getpid(), info.st_dev, info.st_ino, admitted))


def test_depleted_file_survives_new_process_and_replacement(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    result = ctx.Queue()
    results = []
    for _ in range(3):
        process = ctx.Process(target=_host_process, args=(str(tmp_path), result))
        process.start()
        results.append(result.get(timeout=40))
        process.join(timeout=40)
        assert process.exitcode == 0
    assert len({row[0] for row in results}) == 3
    assert len({row[1:3] for row in results}) == 1
    assert [row[3] for row in results] == [10, 0, 0]
    assert next(tmp_path.iterdir()).stat().st_mode & 0o777 == 0o600


def test_simultaneous_cold_processes_admit_one_combined_burst(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    barrier, result = ctx.Barrier(4), ctx.Queue()
    processes = [
        ctx.Process(target=_host_process, args=(str(tmp_path), result, barrier))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    results = [result.get(timeout=40) for _ in processes]
    for process in processes:
        process.join(timeout=40)
        assert process.exitcode == 0
    assert len({row[0] for row in results}) == 4
    assert len({row[1:3] for row in results}) == 1
    assert sum(row[3] for row in results) == 10
