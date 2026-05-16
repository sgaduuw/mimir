"""`update-mainline` — sync Linus's `linux.git` and (re)build the
MAINTAINERS-derived schema + the lore `Link:`-trailer index.

Two independent passes against the local tree:

1. MAINTAINERS: re-parse at HEAD and replace the
   `subsystems` / `subsystem_paths` / `subsystem_maintainers` tables.
   Skipped when HEAD hasn't moved since the last load; `--force`
   overrides.
2. Commit Link-trailer walk: scan every new commit for
   `Link: https://lore.kernel.org/.../<msgid>` trailers and write
   them to `mainline_commits`. Resumable via a separate cursor.

Lives in its own CLI submodule (separate from `mimir.cli.ingest`'s
lkml-side pulls) because the upstream, the target schema, and the
invocation cadence are all distinct.
"""
import subprocess
from pathlib import Path

import click
from sqlalchemy import delete

from mimir import maintainers
from mimir.cli._common import _configure_logging
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import (
    MainlineState,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)


@click.command("update-mainline")
@click.option(
    "--skip-fetch", is_flag=True,
    help="Don't `git fetch` the mainline mirror; just re-read the local HEAD.",
)
@click.option(
    "--skip-maintainers", is_flag=True,
    help="Don't re-parse MAINTAINERS; only walk new commits for Link: trailers.",
)
@click.option(
    "--skip-commits", is_flag=True,
    help="Don't walk commits for Link: trailers; only reload MAINTAINERS.",
)
@click.option(
    "--force", is_flag=True,
    help="Re-parse MAINTAINERS and replace subsystems even if HEAD hasn't moved.",
)
@click.option(
    "-v", "--verbose", count=True,
    help="-v: progress detail. -vv: debug.",
)
def update_mainline_command(
    skip_fetch: bool, skip_maintainers: bool, skip_commits: bool,
    force: bool, verbose: int,
) -> None:
    """Sync the mainline kernel tree (Linus's `linux.git`) and load
    its MAINTAINERS file + index `Link:` trailers from commit
    messages.

    Clones the tree on first run, fetches on subsequent runs. Two
    independent passes against the resulting tree:

    1. MAINTAINERS: read the file at HEAD, replace the
       `subsystems` / `subsystem_paths` / `subsystem_maintainers`
       tables transactionally. Skipped when HEAD hasn't moved
       since the last load (`--force` overrides; for re-runs
       after a parser fix). `--skip-maintainers` disables this
       pass entirely for the tick.

    2. Commit Link-trailer walk: scan every commit since the last
       walker cursor for `Link: https://lore.kernel.org/.../<msgid>`
       trailers, insert into `mainline_commits`. Resumable; the
       second cursor on `MainlineState` advances monotonically.
       First run walks the full history (slow); subsequent ticks
       only see new commits. `--skip-commits` disables this pass.

    The two passes have independent cursors because MAINTAINERS
    only changes when that one file does, but commit-walker has
    new work on almost every tick.
    """
    _configure_logging(verbose)
    tree_path = Path(settings.mainline_tree_path)
    if not tree_path.is_absolute():
        from mimir.config import PROJECT_ROOT
        tree_path = PROJECT_ROOT / tree_path

    if not tree_path.exists():
        tree_path.parent.mkdir(parents=True, exist_ok=True)
        click.echo(f"cloning {settings.mainline_tree_url} -> {tree_path}")
        # `--` stops git from interpreting an option-shaped URL as a
        # flag (same defensive pattern as `mimir.sync.clone_epoch`).
        subprocess.run(
            ["git", "clone", "--mirror", "--",
             settings.mainline_tree_url, str(tree_path)],
            check=True,
        )
    elif not skip_fetch:
        if verbose:
            click.echo(f"fetching {tree_path}")
        subprocess.run(
            ["git", "-C", str(tree_path), "fetch", "--quiet", "--prune"],
            check=True,
        )

    tree_name = "linus"  # fixed slug for now; supports future linux-stable, etc.

    if not skip_maintainers:
        _load_maintainers(tree_path, tree_name, force)

    if not skip_commits:
        _walk_link_trailers(tree_path, tree_name)


def _load_maintainers(tree_path: Path, tree_name: str, force: bool) -> None:
    """Read MAINTAINERS from HEAD and replace the subsystems
    schema. No-op when HEAD matches `last_commit_sha` and not
    forced."""
    from dulwich.repo import Repo
    repo = Repo(str(tree_path))
    head_sha = repo.head().decode("ascii")
    commit = repo[repo.head()]
    tree = repo[commit.tree]
    try:
        _mode, blob_sha = tree[b"MAINTAINERS"]
    except KeyError:
        raise click.ClickException(
            f"no MAINTAINERS file at HEAD of {tree_path}; wrong tree?"
        )
    blob_bytes = repo[blob_sha].data

    with SessionLocal() as session:
        state = session.get(MainlineState, tree_name)
        if state is None:
            state = MainlineState(tree_name=tree_name)
            session.add(state)
        if state.last_commit_sha == head_sha and not force:
            click.echo(
                f"update-mainline: MAINTAINERS unchanged (HEAD "
                f"{head_sha[:12]}); use --force to re-parse"
            )
            return

        parsed = maintainers.parse(blob_bytes)
        # Replace-all in one transaction. The cascade FK on
        # `subsystems.id` clears `subsystem_paths` + `subsystem_maintainers`
        # via ON DELETE CASCADE; SQLite needs `PRAGMA foreign_keys=ON`
        # (set by `mimir.extensions` on every connection) for the
        # cascade to fire.
        session.execute(delete(Subsystem))
        for sub in parsed:
            row = Subsystem(name=sub.name, status=sub.status)
            for path in sub.files:
                row.paths.append(SubsystemPath(glob=path, is_exclude=False))
            for path in sub.excludes:
                row.paths.append(SubsystemPath(glob=path, is_exclude=True))
            for m in sub.maintainers:
                row.maintainers.append(SubsystemMaintainer(
                    role=m.role, name=m.name, address=m.address,
                ))
            session.add(row)
        state.last_commit_sha = head_sha
        session.commit()

    # Invalidate the dynamic-allowlist cache so the web tier picks
    # up the refreshed M:/R: address set on the next request rather
    # than serving stale redaction decisions for up to the cache
    # TTL. The cache table is shared across processes, so this
    # delete from the scheduler sidecar reaches the web container
    # too.
    from mimir import maintainer_allowlist
    maintainer_allowlist.invalidate()

    click.echo(
        f"update-mainline: loaded {len(parsed)} subsystems "
        f"from {tree_name}@{head_sha[:12]}"
    )


def _walk_link_trailers(tree_path: Path, tree_name: str) -> None:
    """Walk new commits, extract `Link:` trailers, insert
    `mainline_commits` rows. Resumable via the cursor on
    `MainlineState.commits_walked_to_sha`."""
    from mimir import mainline
    with SessionLocal() as session:
        result = mainline.walk_commits(session, tree_path, tree_name=tree_name)
    # State-change line at default verbosity. Steady-state ticks
    # produce zero new commits and stay silent.
    if result.commits_seen:
        click.echo(
            f"update-mainline: walked {result.commits_seen} commits "
            f"({result.linked} with lore Link:, {result.rows_inserted} "
            "rows indexed)"
        )
