"""Discover and sync public-inbox v2 epoch repositories from a remote root.

Public-inbox publishes a `manifest.js.gz` at the inbox root listing every
available repo. We fetch that, derive the set of epoch numbers, and:
  - `git clone --mirror` any epoch we don't have locally
  - `git fetch` existing local epochs to pull new commits

Both clone and fetch shell out to `git` — its smart-HTTP transport is
faster and more reliable than dulwich's HTTP client for large repos.
"""
import gzip
import json
import logging
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SyncResult(BaseModel):
    cloned: list[str] = []
    fetched: list[str] = []
    failed: list[str] = []


def _user_agent() -> str:
    """Builds the UA string at call time so the version pinned by
    `importlib.metadata` in `mimir.__init__` is read (instead of a
    hardcoded `0.1` that drifts against every release). Resolved
    lazily to dodge a circular-import edge during package init."""
    from mimir import __version__
    return f"mimir/{__version__} (+https://github.com/sgaduuw/mimir)"


def fetch_manifest(upstream_url: str) -> dict:
    url = upstream_url.rstrip("/") + "/manifest.js.gz"
    logger.info("fetching manifest %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
    return json.loads(gzip.decompress(raw))


def discover_remote_epochs(upstream_url: str) -> list[tuple[str, str]]:
    """Return sorted list of (epoch_name, clone_url) tuples.

    Real-world example for lkml: keys are `/lkml/git/<N>.git`. We strip the
    `.git` suffix to get the epoch name and prepend the URL origin to the
    manifest key to get the clone URL.
    """
    manifest = fetch_manifest(upstream_url)
    parsed = urlparse(upstream_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    upstream_prefix = parsed.path.rstrip("/") + "/"

    out: list[tuple[str, str]] = []
    for key in manifest.keys():
        if not key.startswith(upstream_prefix):
            continue
        name = key.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name.isdigit():
            continue
        out.append((name, origin + key))
    return sorted(out, key=lambda nu: int(nu[0]))


def local_epoch_path(mirror_path: Path, name: str) -> Path | None:
    """Return the on-disk path for epoch `name` if present, else None.

    Accepts either `<name>.git` (bare/mirror) or `<name>` (working tree)
    layouts.
    """
    bare = mirror_path / f"{name}.git"
    if bare.exists():
        return bare
    plain = mirror_path / name
    if plain.exists():
        return plain
    return None


def local_epoch_names(mirror_path: Path) -> set[str]:
    if not mirror_path.exists():
        return set()
    names: set[str] = set()
    for child in mirror_path.iterdir():
        if not child.is_dir():
            continue
        n = child.name[:-4] if child.name.endswith(".git") else child.name
        if n.isdigit():
            names.add(n)
    return names


def clone_epoch(name: str, clone_url: str, mirror_path: Path) -> Path:
    target = mirror_path / f"{name}.git"
    mirror_path.mkdir(parents=True, exist_ok=True)
    logger.info("cloning epoch %s from %s -> %s", name, clone_url, target)
    # `--` separates options from positional args; without it, a
    # hostile manifest could smuggle an option-shaped clone_url like
    # `--upload-pack=...` into git's argv.
    subprocess.run(
        ["git", "clone", "--mirror", "--", clone_url, str(target)],
        check=True,
    )
    return target


def fetch_epoch(epoch_path: Path) -> None:
    logger.info("fetching epoch repo %s", epoch_path)
    subprocess.run(
        ["git", "-C", str(epoch_path), "fetch", "--quiet", "--prune"],
        check=True,
    )


def sync_epochs(
    upstream_url: str,
    mirror_path: Path,
    discover_new: bool = True,
    fetch_existing: bool = True,
) -> SyncResult:
    """Discover new epochs, clone them, and fetch updates on existing ones."""
    result = SyncResult()
    local = local_epoch_names(mirror_path)

    remote: list[tuple[str, str]] = []
    if discover_new:
        try:
            remote = discover_remote_epochs(upstream_url)
        except Exception as exc:
            logger.error("manifest fetch failed: %r", exc)

    new_to_clone = [(n, u) for n, u in remote if n not in local]
    for name, clone_url in new_to_clone:
        try:
            clone_epoch(name, clone_url, mirror_path)
            result.cloned.append(name)
            local.add(name)
        except subprocess.CalledProcessError as exc:
            logger.error("clone of epoch %s failed: %r", name, exc)
            result.failed.append(name)

    if fetch_existing:
        for name in sorted(local, key=int):
            path = local_epoch_path(mirror_path, name)
            if path is None:
                continue
            try:
                fetch_epoch(path)
                result.fetched.append(name)
            except subprocess.CalledProcessError as exc:
                logger.error("fetch of epoch %s failed: %r", name, exc)
                result.failed.append(name)

    return result
