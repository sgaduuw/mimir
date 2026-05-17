"""Public-inbox manifest parsing + epoch sync orchestration.

`mimir.sync` was missing all test coverage until now -- the CLI tests
that exercise `update` mock the sync call wholesale, so the actual
manifest interpretation, local-epoch discovery, and clone/fetch wiring
had no guard against a regression.

Network and subprocess are mocked at module-import time -- the
manifest format is the actual contract we care about, and the
git-subprocess invocation is the secondary contract (argv shape).
The network mock patches `OUTBOUND_OPENER.open` (the hardened
opener used by the production caller) rather than `urlopen`
directly, so the no-redirect handler stays in the call path that
the tests reason about.
"""
import gzip
import io
import json
import subprocess
from unittest.mock import MagicMock, patch


import pytest

import mimir.sync as sync_module
from mimir.sync import (
    SyncResult,
    clone_epoch,
    discover_remote_epochs,
    fetch_epoch,
    fetch_manifest,
    local_epoch_names,
    local_epoch_path,
    sync_epochs,
)


def _gzipped_json(payload: dict) -> bytes:
    """Encode a dict the way public-inbox publishes manifest.js.gz."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="w") as gz:
        gz.write(json.dumps(payload).encode())
    return buf.getvalue()


# fetch_manifest -- the on-the-wire format is gzipped JSON.


def test_fetch_manifest_decompresses_and_parses():
    """The manifest is served as gzipped JSON; the helper must
    transparently decompress and parse to a dict. A regression
    that returned bytes or didn't decompress would surface as a
    JSONDecodeError or a TypeError downstream."""
    payload = {"/lkml/git/0.git": {"description": "first epoch"}}
    fake_response = MagicMock()
    fake_response.read.return_value = _gzipped_json(payload)
    fake_response.__enter__ = lambda self: fake_response
    fake_response.__exit__ = lambda *args: False

    with patch.object(sync_module.OUTBOUND_OPENER, "open", return_value=fake_response):
        out = fetch_manifest("https://lore.kernel.org/lkml")

    assert out == payload


def test_fetch_manifest_sends_user_agent():
    """The User-Agent string identifies mimir to upstream servers
    so they can rate-limit or block us individually. A regression
    that lost the UA would make us look like an anonymous scraper."""
    payload = {}
    fake_response = MagicMock()
    fake_response.read.return_value = _gzipped_json(payload)
    fake_response.__enter__ = lambda self: fake_response
    fake_response.__exit__ = lambda *args: False

    captured_request = []

    def _urlopen(req, **_kwargs):
        captured_request.append(req)
        return fake_response

    with patch.object(sync_module.OUTBOUND_OPENER, "open", side_effect=_urlopen):
        fetch_manifest("https://lore.kernel.org/lkml")

    assert len(captured_request) == 1
    ua = captured_request[0].get_header("User-agent") or ""
    assert "mimir" in ua.lower()


def test_fetch_manifest_passes_timeout_to_urlopen():
    """A hostile or hung upstream must not stall the scheduler tick;
    `urlopen` is invoked with the module's wall-clock cap. A regression
    that dropped the timeout kwarg would silently restore the
    indefinite-hang shape this hardens against."""
    payload = {}
    fake_response = MagicMock()
    fake_response.read.return_value = _gzipped_json(payload)
    fake_response.__enter__ = lambda self: fake_response
    fake_response.__exit__ = lambda *args: False

    captured_kwargs = []

    def _urlopen(req, **kwargs):
        captured_kwargs.append(kwargs)
        return fake_response

    with patch.object(sync_module.OUTBOUND_OPENER, "open", side_effect=_urlopen):
        fetch_manifest("https://lore.kernel.org/lkml")

    assert captured_kwargs[0].get("timeout") == sync_module._MANIFEST_TIMEOUT_SEC


def test_fetch_manifest_caps_oversized_response():
    """An upstream that returns more than the cap (corrupt mirror,
    misconfigured server, hostile actor) raises rather than buffering
    a giant payload into memory. The trailing-byte read idiom means
    we detect overflow via length comparison, not Content-Length."""
    oversize = b"x" * (sync_module._MANIFEST_MAX_BYTES + 1)
    fake_response = MagicMock()
    fake_response.read.return_value = oversize
    fake_response.__enter__ = lambda self: fake_response
    fake_response.__exit__ = lambda *args: False

    with patch.object(sync_module.OUTBOUND_OPENER, "open", return_value=fake_response):
        with pytest.raises(ValueError, match="exceeds"):
            fetch_manifest("https://lore.kernel.org/lkml")


def test_fetch_manifest_uses_manifest_js_gz_suffix():
    """The URL path appended to the upstream base is `/manifest.js.gz`
    -- a typo to `manifest.json.gz` or `manifest.gz` would silently
    404 every sync."""
    payload = {}
    fake_response = MagicMock()
    fake_response.read.return_value = _gzipped_json(payload)
    fake_response.__enter__ = lambda self: fake_response
    fake_response.__exit__ = lambda *args: False

    captured = []

    def _urlopen(req, **_kwargs):
        captured.append(req.full_url)
        return fake_response

    with patch.object(sync_module.OUTBOUND_OPENER, "open", side_effect=_urlopen):
        fetch_manifest("https://lore.kernel.org/lkml")
        fetch_manifest("https://lore.kernel.org/lkml/")  # trailing slash

    assert captured == [
        "https://lore.kernel.org/lkml/manifest.js.gz",
        "https://lore.kernel.org/lkml/manifest.js.gz",
    ]


# discover_remote_epochs -- the manifest-to-epoch-list translation.


def _patch_manifest(manifest: dict):
    """Patch `fetch_manifest` to return the given dict directly,
    bypassing the network. Tests that exercise the parsing logic
    don't care about the gzipped-JSON encoding step."""
    return patch("mimir.sync.fetch_manifest", return_value=manifest)


def test_discover_remote_epochs_extracts_name_and_clone_url():
    """A realistic manifest entry has `/lkml/git/N.git` keys; the
    helper strips the `.git` suffix to get the epoch name and
    composes the clone URL from the origin + key."""
    manifest = {
        "/lkml/git/0.git": {},
        "/lkml/git/1.git": {},
        "/lkml/git/2.git": {},
    }
    with _patch_manifest(manifest):
        out = discover_remote_epochs("https://lore.kernel.org/lkml")
    assert out == [
        ("0", "https://lore.kernel.org/lkml/git/0.git"),
        ("1", "https://lore.kernel.org/lkml/git/1.git"),
        ("2", "https://lore.kernel.org/lkml/git/2.git"),
    ]


def test_discover_remote_epochs_sorts_numerically_not_lexicographically():
    """Real archives have 10+ epochs; lexicographic sort would put
    `10` before `2`. Numeric sort by `int(name)` is the contract."""
    manifest = {f"/lkml/git/{n}.git": {} for n in (0, 1, 2, 9, 10, 11)}
    with _patch_manifest(manifest):
        out = discover_remote_epochs("https://lore.kernel.org/lkml")
    names = [n for n, _ in out]
    assert names == ["0", "1", "2", "9", "10", "11"]


def test_discover_remote_epochs_filters_non_matching_prefix():
    """Some upstreams host multiple inboxes from a shared manifest
    (lore.kernel.org's union manifest carries every inbox). Keys
    outside the requested inbox prefix must be filtered out."""
    manifest = {
        "/lkml/git/0.git": {},
        "/linux-fsdevel/git/0.git": {},  # different inbox
        "/lkml/git/1.git": {},
        "/oss-security/git/0.git": {},
    }
    with _patch_manifest(manifest):
        out = discover_remote_epochs("https://lore.kernel.org/lkml")
    names = [n for n, _ in out]
    assert names == ["0", "1"]
    # No cross-inbox URL leakage.
    for _, url in out:
        assert "/lkml/git/" in url


def test_discover_remote_epochs_skips_non_numeric_names():
    """Some manifest keys are non-epoch entries (e.g. `_/git/foo`
    or topic refs). Only digit-named entries are valid epochs --
    parsing a `description` or `refs` key as an epoch would later
    crash on `int(name)`."""
    manifest = {
        "/lkml/git/0.git": {},
        "/lkml/git/foo.git": {},  # not an epoch
        "/lkml/git/1.git": {},
        "/lkml/git/_extra": {},  # not even .git
    }
    with _patch_manifest(manifest):
        out = discover_remote_epochs("https://lore.kernel.org/lkml")
    names = [n for n, _ in out]
    assert names == ["0", "1"]


def test_discover_remote_epochs_handles_url_with_trailing_slash():
    """Operator typo: `https://lore.kernel.org/lkml/` vs without.
    Both must yield the same epoch set; the urlparse strip handles it."""
    manifest = {"/lkml/git/0.git": {}, "/lkml/git/1.git": {}}
    with _patch_manifest(manifest):
        out_no_slash = discover_remote_epochs("https://lore.kernel.org/lkml")
        out_slash = discover_remote_epochs("https://lore.kernel.org/lkml/")
    assert out_no_slash == out_slash


def test_discover_remote_epochs_empty_manifest():
    with _patch_manifest({}):
        out = discover_remote_epochs("https://lore.kernel.org/lkml")
    assert out == []


# local_epoch_path / local_epoch_names -- on-disk layout discovery.


def test_local_epoch_path_returns_bare_layout(tmp_path):
    """The standard public-inbox mirror layout is bare repos
    `<N>.git`; `local_epoch_path` returns that path."""
    (tmp_path / "0.git").mkdir()
    assert local_epoch_path(tmp_path, "0") == tmp_path / "0.git"


def test_local_epoch_path_returns_plain_layout(tmp_path):
    """Some operators clone non-bare (`<N>` without `.git`); the
    helper accepts both layouts so a user-set mirror_path still
    works whichever way it was cloned."""
    (tmp_path / "5").mkdir()
    assert local_epoch_path(tmp_path, "5") == tmp_path / "5"


def test_local_epoch_path_prefers_bare_when_both_present(tmp_path):
    """If both layouts exist (operator error: a stray non-bare dir
    next to the real mirror), prefer the bare form -- it's the
    canonical public-inbox layout and is what `git fetch` knows
    how to work with."""
    (tmp_path / "0.git").mkdir()
    (tmp_path / "0").mkdir()
    assert local_epoch_path(tmp_path, "0") == tmp_path / "0.git"


def test_local_epoch_path_returns_none_when_missing(tmp_path):
    assert local_epoch_path(tmp_path, "99") is None


def test_local_epoch_names_lists_numeric_dirs(tmp_path):
    """`local_epoch_names` returns the set of digit-named directories
    (both `<N>.git` and `<N>`). Non-numeric and non-directory entries
    are filtered out."""
    (tmp_path / "0.git").mkdir()
    (tmp_path / "1.git").mkdir()
    (tmp_path / "2").mkdir()  # plain layout
    (tmp_path / "notes.git").mkdir()  # not an epoch
    (tmp_path / "manifest.js.gz").write_bytes(b"")  # file, not dir
    assert local_epoch_names(tmp_path) == {"0", "1", "2"}


def test_local_epoch_names_handles_missing_mirror_dir(tmp_path):
    """A brand-new inbox has no mirror_path yet; the helper must
    return an empty set rather than raise."""
    assert local_epoch_names(tmp_path / "does-not-exist") == set()


# clone_epoch / fetch_epoch -- subprocess wiring.


def test_clone_epoch_invokes_git_with_mirror_flag(tmp_path):
    """`git clone --mirror` is the only safe way to mirror a
    public-inbox v2 archive; a refactor that lost `--mirror` would
    create a working tree that `git fetch` can't update properly."""
    captured = []
    with patch("mimir.sync.subprocess.run") as run:
        clone_epoch("0", "https://example.test/lkml/git/0.git", tmp_path)
        captured.append(run.call_args)

    args = captured[0].args[0]
    assert args[0] == "git"
    assert args[1] == "clone"
    assert "--mirror" in args
    # `--` separator must come before the positional clone_url to
    # block a hostile manifest from smuggling option-shaped URLs.
    sep_idx = args.index("--")
    url_idx = args.index("https://example.test/lkml/git/0.git")
    target_idx = args.index(str(tmp_path / "0.git"))
    assert sep_idx < url_idx < target_idx


def test_clone_epoch_passes_timeout_to_subprocess(tmp_path):
    """A hung `git clone` (dead connection, malicious smart-HTTP
    server) must surface as TimeoutExpired rather than parking the
    scheduler indefinitely. The kwarg is what makes that possible;
    a regression that dropped it would silently restore the hang."""
    with patch("mimir.sync.subprocess.run") as run:
        clone_epoch("0", "https://example.test/lkml/git/0.git", tmp_path)
    assert run.call_args.kwargs.get("timeout") == sync_module._GIT_CLONE_TIMEOUT_SEC


def test_clone_epoch_creates_mirror_dir(tmp_path):
    """If the parent mirror_path doesn't exist yet (first ingest of
    a new inbox), `clone_epoch` creates it so git doesn't fail with
    "destination path's parent does not exist"."""
    missing = tmp_path / "new-mirror"
    assert not missing.exists()
    with patch("mimir.sync.subprocess.run"):
        clone_epoch("0", "https://example.test/lkml/git/0.git", missing)
    assert missing.exists() and missing.is_dir()


def test_fetch_epoch_invokes_git_fetch_in_path(tmp_path):
    """`git -C <path> fetch` runs in the epoch dir without changing
    cwd. The `--quiet --prune` flags keep logs clean and drop
    stale remote refs."""
    epoch = tmp_path / "0.git"
    epoch.mkdir()
    with patch("mimir.sync.subprocess.run") as run:
        fetch_epoch(epoch)

    args = run.call_args.args[0]
    assert args[:3] == ["git", "-C", str(epoch)]
    assert "fetch" in args
    assert "--prune" in args


def test_fetch_epoch_passes_timeout_to_subprocess(tmp_path):
    """Symmetric to the clone-timeout test: an incremental fetch
    against a hung remote must time out rather than block."""
    epoch = tmp_path / "0.git"
    epoch.mkdir()
    with patch("mimir.sync.subprocess.run") as run:
        fetch_epoch(epoch)
    assert run.call_args.kwargs.get("timeout") == sync_module._GIT_FETCH_TIMEOUT_SEC


# sync_epochs -- the orchestrator that ties it all together.


def test_sync_epochs_clones_new_and_fetches_existing(tmp_path):
    """The orchestrator's golden path: discovers two remote epochs,
    one of which is already local. Clones the missing one, fetches
    the existing one, and reports each in the right SyncResult
    bucket."""
    (tmp_path / "0.git").mkdir()  # epoch 0 already local
    remote = [
        ("0", "https://example.test/lkml/git/0.git"),
        ("1", "https://example.test/lkml/git/1.git"),
    ]

    def _clone(name, url, mirror_path):
        (mirror_path / f"{name}.git").mkdir()
        return mirror_path / f"{name}.git"

    with patch("mimir.sync.discover_remote_epochs", return_value=remote), \
         patch("mimir.sync.clone_epoch", side_effect=_clone) as clone, \
         patch("mimir.sync.fetch_epoch") as fetch:
        result = sync_epochs("https://example.test/lkml", tmp_path)

    assert isinstance(result, SyncResult)
    assert result.cloned == ["1"]
    # Both epochs get fetched at the end (cloned epoch is now local).
    assert sorted(result.fetched) == ["0", "1"]
    assert result.failed == []
    # clone_epoch was called exactly once, for the missing epoch.
    assert clone.call_count == 1
    assert clone.call_args.args[0] == "1"
    # fetch_epoch was called for both.
    assert fetch.call_count == 2


def test_sync_epochs_captures_clone_failure_in_result(tmp_path):
    """When `git clone` fails (network, auth, dead manifest URL), the
    failing epoch name goes into result.failed and the orchestrator
    keeps going -- one broken epoch must not block the others."""
    remote = [
        ("0", "https://example.test/lkml/git/0.git"),
        ("1", "https://example.test/lkml/git/1.git"),
    ]
    call_count = 0

    def _clone(name, url, mirror_path):
        nonlocal call_count
        call_count += 1
        if name == "0":
            raise subprocess.CalledProcessError(128, ["git"])
        (mirror_path / f"{name}.git").mkdir()  # simulate successful clone
        return mirror_path / f"{name}.git"

    with patch("mimir.sync.discover_remote_epochs", return_value=remote), \
         patch("mimir.sync.clone_epoch", side_effect=_clone), \
         patch("mimir.sync.fetch_epoch"):
        result = sync_epochs("https://example.test/lkml", tmp_path)

    assert call_count == 2  # both attempted, despite the first failing
    assert result.cloned == ["1"]
    assert "0" in result.failed
    # Successful clone gets fetched at the end; the failed one was
    # never added to `local`, so it isn't.
    assert "1" in result.fetched
    assert "0" not in result.fetched


def test_sync_epochs_captures_clone_timeout_in_result(tmp_path):
    """A wall-clock timeout on `git clone` raises `TimeoutExpired`,
    not `CalledProcessError`. The orchestrator must catch the
    broader `SubprocessError` so a single hung clone goes into
    result.failed rather than crashing the tick."""
    remote = [("0", "https://example.test/lkml/git/0.git")]

    def _clone(name, url, mirror_path):
        raise subprocess.TimeoutExpired(["git"], timeout=1)

    with patch("mimir.sync.discover_remote_epochs", return_value=remote), \
         patch("mimir.sync.clone_epoch", side_effect=_clone), \
         patch("mimir.sync.fetch_epoch"):
        result = sync_epochs("https://example.test/lkml", tmp_path)

    assert "0" in result.failed
    assert result.cloned == []


def test_sync_epochs_captures_fetch_timeout_in_result(tmp_path):
    """Symmetric to the clone-timeout case: a hung `git fetch` must
    be captured in result.failed rather than crashing the tick."""
    (tmp_path / "0.git").mkdir()

    def _fetch(path):
        raise subprocess.TimeoutExpired(["git"], timeout=1)

    with patch("mimir.sync.discover_remote_epochs", return_value=[]), \
         patch("mimir.sync.clone_epoch"), \
         patch("mimir.sync.fetch_epoch", side_effect=_fetch):
        result = sync_epochs("https://example.test/lkml", tmp_path)

    assert "0" in result.failed


def test_sync_epochs_captures_fetch_failure_in_result(tmp_path):
    """A `git fetch` failure on an existing epoch must NOT take down
    the orchestrator; it's captured in result.failed and other
    epochs continue. This is the path the scheduler hits on every
    update tick, so robustness here matters."""
    (tmp_path / "0.git").mkdir()
    (tmp_path / "1.git").mkdir()

    def _fetch(path):
        if path.name == "0.git":
            raise subprocess.CalledProcessError(128, ["git"])

    with patch("mimir.sync.discover_remote_epochs", return_value=[]), \
         patch("mimir.sync.clone_epoch"), \
         patch("mimir.sync.fetch_epoch", side_effect=_fetch):
        result = sync_epochs("https://example.test/lkml", tmp_path)

    assert "0" in result.failed
    assert "1" in result.fetched


def test_sync_epochs_swallows_manifest_fetch_failure(tmp_path):
    """If the upstream manifest is unreachable, `sync_epochs` logs
    and skips the clone-new-epochs phase but still fetches what we
    have locally. This is what keeps an offline sidecar from
    crashing the whole scheduler tick."""
    (tmp_path / "0.git").mkdir()

    def _discover_raises(_url):
        raise OSError("unreachable")

    with patch("mimir.sync.discover_remote_epochs", side_effect=_discover_raises), \
         patch("mimir.sync.clone_epoch") as clone, \
         patch("mimir.sync.fetch_epoch") as fetch:
        result = sync_epochs("https://example.test/lkml", tmp_path)

    # Nothing cloned (we couldn't enumerate remotes).
    clone.assert_not_called()
    assert result.cloned == []
    # Local epoch still gets fetched -- the discovery failure must
    # not block the local-update half of the contract.
    fetch.assert_called_once()
    assert result.fetched == ["0"]


def test_sync_epochs_discover_new_disabled(tmp_path):
    """`discover_new=False` skips the manifest fetch entirely. Used
    by `update --no-discover` (or its equivalent) when an operator
    knows the local epoch set is complete and just wants to pull
    new commits."""
    (tmp_path / "0.git").mkdir()
    with patch("mimir.sync.discover_remote_epochs") as discover, \
         patch("mimir.sync.fetch_epoch"):
        result = sync_epochs(
            "https://example.test/lkml", tmp_path, discover_new=False,
        )

    discover.assert_not_called()
    assert result.cloned == []
    assert result.fetched == ["0"]


def test_sync_epochs_fetch_existing_disabled(tmp_path):
    """`fetch_existing=False` skips fetching local epochs (clone-only
    mode). Used in narrow scenarios -- e.g. an operator who runs
    `git fetch` out-of-band and wants `sync_epochs` to only handle
    the discover+clone half."""
    remote = [("1", "https://example.test/lkml/git/1.git")]
    (tmp_path / "0.git").mkdir()  # existing local epoch
    with patch("mimir.sync.discover_remote_epochs", return_value=remote), \
         patch("mimir.sync.clone_epoch") as clone, \
         patch("mimir.sync.fetch_epoch") as fetch:
        result = sync_epochs(
            "https://example.test/lkml", tmp_path, fetch_existing=False,
        )

    clone.assert_called_once()
    fetch.assert_not_called()
    assert result.cloned == ["1"]
    assert result.fetched == []
