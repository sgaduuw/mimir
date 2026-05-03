"""Disk-backed cache for cross-process sharing.

The Flask server, the `warm-cache` CLI, and any other process all share
a single pickle file at `Settings.cache_path`. Writers go through atomic
rename so concurrent reads see a consistent file. There's no in-memory
layer: disk reads of a small pickle are sub-millisecond, which is below
our latency budget; the simplicity is worth more than the saved
microseconds.

Pickle (not JSON) because cached values include datetime objects and
custom dataclasses that don't round-trip cleanly through JSON. Pickle is
safe here because we control all writers.

Concurrent multi-process writes have a benign last-writer-wins race:
both processes load, mutate, and save; the last save overwrites the
other. Lost cache writes just mean the next request recomputes — no
corruption, no functional impact at our scale.
"""
import pickle
import time
from threading import Lock

from mimir.config import settings

_lock = Lock()


def _load() -> dict:
    p = settings.cache_path
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            return pickle.load(f)
    except (OSError, pickle.PickleError, EOFError):
        return {}


def _save(data: dict) -> None:
    p = settings.cache_path
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("wb") as f:
        pickle.dump(data, f)
    tmp.replace(p)


def get(key: str):
    with _lock:
        data = _load()
    entry = data.get(key)
    if entry is None:
        return None
    if entry["expires_at"] < time.time():
        return None
    return entry["value"]


def set(key: str, value, ttl: int) -> None:
    with _lock:
        data = _load()
        data[key] = {"value": value, "expires_at": time.time() + ttl}
        _save(data)


def keys() -> list[str]:
    """Non-expired keys in the cache. Useful for `warm-cache` reporting."""
    now = time.time()
    with _lock:
        data = _load()
    return [k for k, entry in data.items() if entry["expires_at"] >= now]
