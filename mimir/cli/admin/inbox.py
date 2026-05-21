"""`admin inbox …`: CRUD on the `inboxes` table + per-inbox trackers.

Thin click wrappers around `mimir.inboxes`'s service-layer
functions (validation, cascade-delete semantics, nav-name cache
refresh all live there). Mutating commands route through the broker
when `BROKER_SOCKET_PATH` is set, matching the rest of the broker
family; read commands (`list`, `show`, `trackers show`) hit the DB
directly via `query_only=1` connections.

Broker error mapping: the service-layer `InboxNotFound` /
`InboxValidationError` arrive over the wire as
`Reply(ok=False, error="InboxNotFound:<msg>")` /
`error="InvalidInbox:<msg>"`. `_raise_for_inbox_error` parses the
prefix and re-raises as the right `ClickException` so operator-
facing output stays the same as the direct path.
"""

from pathlib import Path

import click
from sqlalchemy import func, select

from mimir.cli._common import _parse_pair
from mimir.cli.admin import admin_group
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.inboxes import (
    InboxNotFound,
    InboxValidationError,
    add_tracked_author,
    clear_tracked_authors,
    create_inbox,
    delete_inbox,
    get_inbox,
    list_inboxes,
    remove_tracked_author,
    set_tracked_authors,
    update_inbox,
)
from mimir.models import ArticleList, IngestState


def _broker_dispatch_enabled() -> bool:
    """True iff this CLI invocation should route mutating ops via the
    broker. Centralises the check so every command shares one rule."""
    return settings.broker_socket_path is not None


def _inbox_click_error(exc) -> click.ClickException:
    """Given a `BrokerUnavailable` raised by an `inbox_*` client
    method, extract the structured error portion and return a
    `ClickException` matching the direct-path text.

    The client wraps the broker's `Reply.error` as
    `"inbox_xxx: <reply.error>"`. The reply error is either:

    - `"InboxNotFound:<msg>"` → re-raise as bare `<msg>`
    - `"InvalidInbox:<msg>"` → re-raise as bare `<msg>`
    - anything else → leave the full string visible

    Keeps `admin inbox add`'s "inbox 'foo' already exists" output
    identical between broker and direct paths."""
    raw = str(exc)
    _, _, reply_error = raw.partition(": ")
    if reply_error.startswith("InboxNotFound:"):
        return click.ClickException(reply_error[len("InboxNotFound:") :])
    if reply_error.startswith("InvalidInbox:"):
        return click.ClickException(reply_error[len("InvalidInbox:") :])
    return click.ClickException(raw)


@admin_group.group("inbox")
def admin_inbox_group() -> None:
    """CRUD on the `inboxes` table.

    These commands are the CLI front-end to the same service-layer
    functions the future Flask admin UI will call. Validation,
    cascade-delete semantics, and the nav-name cache refresh all live
    in `mimir.inboxes`.
    """


@admin_inbox_group.command("list")
def admin_inbox_list_command() -> None:
    """List every configured inbox with its mirror path and upstream URL."""
    inboxes = list_inboxes()
    if not inboxes:
        click.echo("(no inboxes)")
        return
    name_w = max(len(ix.name) for ix in inboxes)
    path_w = max(len(ix.mirror_path) for ix in inboxes)
    for ix in inboxes:
        n = len(ix.tracked_authors or {})
        trackers = f"trackers={n}" if n else "trackers=none"
        click.echo(
            f"{ix.id:>4}  {ix.name:<{name_w}}  "
            f"{ix.mirror_path:<{path_w}}  {ix.upstream_url}  {trackers}"
        )


@admin_inbox_group.command("show")
@click.argument("name")
def admin_inbox_show_command(name: str) -> None:
    """Detail view for one inbox: config + per-epoch ingest cursors."""
    try:
        inbox = get_inbox(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))

    click.echo(f"id:           {inbox.id}")
    click.echo(f"name:         {inbox.name}")
    click.echo(f"mirror_path:  {inbox.mirror_path}")
    click.echo(f"upstream_url: {inbox.upstream_url}")

    with SessionLocal() as session:
        states = (
            session.execute(
                select(IngestState)
                .where(IngestState.inbox_id == inbox.id)
                .order_by(IngestState.epoch)
            )
            .scalars()
            .all()
        )
        article_count = session.execute(
            select(func.count())
            .select_from(ArticleList)
            .where(ArticleList.inbox_id == inbox.id)
        ).scalar_one()
    click.echo(f"linked articles: {article_count}")
    if states:
        click.echo("ingest cursors:")
        for s in states:
            head = s.last_commit_sha or "<beginning>"
            click.echo(f"  {s.epoch}: {head}")
    else:
        click.echo("ingest cursors: (none, never ingested)")


@admin_inbox_group.command("add")
@click.argument("name")
@click.option(
    "--mirror-path",
    default=None,
    help="Filesystem path to the public-inbox mirror root. "
    "Defaults to Inboxes/<name>/git.",
)
@click.option(
    "--upstream-url",
    default=None,
    help="Upstream public-inbox URL (https://...). "
    "Defaults to https://lore.kernel.org/<name>.",
)
def admin_inbox_add_command(
    name: str,
    mirror_path: str | None,
    upstream_url: str | None,
) -> None:
    """Insert a new inbox.

    With only NAME, defaults to `Inboxes/<name>/git` on disk and
    `https://lore.kernel.org/<name>` upstream, matching the conventional
    lore.kernel.org public-inbox layout. Pass --mirror-path /
    --upstream-url to override either independently. Run
    `flask --app mimir update --inbox <name>` afterwards to clone the
    upstream mirror and ingest.
    """
    if mirror_path is None:
        mirror_path = f"Inboxes/{name}/git"
    if upstream_url is None:
        upstream_url = f"https://lore.kernel.org/{name}"

    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            inbox_dict = get_broker_client().inbox_create(
                name,
                mirror_path,
                upstream_url,
            )
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        click.echo(f"created inbox {inbox_dict['name']!r} (id={inbox_dict['id']})")
        click.echo(f"  mirror_path:  {inbox_dict['mirror_path']}")
        click.echo(f"  upstream_url: {inbox_dict['upstream_url']}")
        click.echo(
            f"next: poetry run flask --app mimir update --inbox {inbox_dict['name']}"
        )
        return

    try:
        inbox = create_inbox(name, mirror_path=mirror_path, upstream_url=upstream_url)
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"created inbox {inbox.name!r} (id={inbox.id})")
    click.echo(f"  mirror_path:  {inbox.mirror_path}")
    click.echo(f"  upstream_url: {inbox.upstream_url}")
    click.echo(f"next: poetry run flask --app mimir update --inbox {inbox.name}")


@admin_inbox_group.command("update")
@click.argument("name")
@click.option("--mirror-path", default=None, help="New filesystem path.")
@click.option("--upstream-url", default=None, help="New upstream URL.")
@click.option(
    "--rename",
    "new_name",
    default=None,
    help="Rename to NEW_NAME (changes URL slug + cache keys).",
)
def admin_inbox_update_command(
    name: str,
    mirror_path: str | None,
    upstream_url: str | None,
    new_name: str | None,
) -> None:
    """Modify an existing inbox. Only the supplied fields are touched."""
    if mirror_path is None and upstream_url is None and new_name is None:
        raise click.ClickException(
            "nothing to update, pass at least one of "
            "--mirror-path / --upstream-url / --rename"
        )

    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            inbox_dict = get_broker_client().inbox_update(
                name,
                new_name=new_name,
                mirror_path=mirror_path,
                upstream_url=upstream_url,
            )
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        click.echo(f"updated inbox {inbox_dict['name']!r}")
        click.echo(f"  mirror_path:  {inbox_dict['mirror_path']}")
        click.echo(f"  upstream_url: {inbox_dict['upstream_url']}")
        return

    try:
        inbox = update_inbox(
            name,
            new_name=new_name,
            mirror_path=mirror_path,
            upstream_url=upstream_url,
        )
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"updated inbox {inbox.name!r}")
    click.echo(f"  mirror_path:  {inbox.mirror_path}")
    click.echo(f"  upstream_url: {inbox.upstream_url}")


@admin_inbox_group.command("remove")
@click.argument("name")
@click.option(
    "--keep-orphan-articles",
    is_flag=True,
    help="Keep articles that lose their last inbox link. Default is to "
    "delete them (other inboxes' cross-posts are unaffected).",
)
@click.option(
    "--remove-inbox-data",
    is_flag=True,
    help="Also delete the on-disk public-inbox mirror at <mirror_path>. "
    "Permanent, re-cloning takes hours and ~20 GB for lkml. Prompts "
    "for confirmation.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def admin_inbox_remove_command(
    name: str,
    keep_orphan_articles: bool,
    remove_inbox_data: bool,
    yes: bool,
) -> None:
    """Delete an inbox and its dependent rows.

    Cascades: removes article_lists + ingest_state rows for this inbox.
    By default also deletes any articles left without remaining links.
    Cross-posts to other inboxes are unaffected.
    """
    # The prompts run client-side regardless of dispatch mode. The
    # mirror-path resolution for the "will rm -rf X" preview lookup
    # needs `get_inbox`, which is a read; safe under both paths.
    try:
        inbox = get_inbox(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))

    if remove_inbox_data:
        path = Path(inbox.mirror_path)
        target = path.parent if path.name == "git" else path
        click.echo(f"--remove-inbox-data set: will rm -rf {target}")
        if target.exists() and not yes:
            click.confirm(
                f"DELETE the on-disk mirror at {target}?",
                abort=True,
            )

    if not yes:
        click.confirm(
            f"Remove inbox {name!r} from the database?",
            abort=True,
        )

    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            report_dict = get_broker_client().inbox_delete(
                name,
                keep_orphan_articles=keep_orphan_articles,
                remove_inbox_data=remove_inbox_data,
            )
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        click.echo(f"removed inbox {report_dict['name']!r}")
        click.echo(
            f"  article_lists rows deleted: {report_dict['article_lists_deleted']}"
        )
        click.echo(
            f"  ingest_state rows deleted:  {report_dict['ingest_state_deleted']}"
        )
        if not keep_orphan_articles:
            click.echo(
                f"  orphan articles deleted:    "
                f"{report_dict['orphan_articles_deleted']}"
            )
        if report_dict.get("mirror_path_deleted"):
            click.echo(
                f"  removed on-disk mirror:     {report_dict['mirror_path_deleted']}"
            )
        return

    report = delete_inbox(
        name,
        keep_orphan_articles=keep_orphan_articles,
        remove_inbox_data=remove_inbox_data,
    )
    click.echo(f"removed inbox {report.name!r}")
    click.echo(f"  article_lists rows deleted: {report.article_lists_deleted}")
    click.echo(f"  ingest_state rows deleted:  {report.ingest_state_deleted}")
    if not keep_orphan_articles:
        click.echo(f"  orphan articles deleted:    {report.orphan_articles_deleted}")
    if report.mirror_path_deleted:
        click.echo(f"  removed on-disk mirror:     {report.mirror_path_deleted}")


@admin_inbox_group.group("trackers")
def admin_inbox_trackers_group() -> None:
    """Per-inbox author trackers shown on the inbox dashboard.

    Each tracker is a (label, email-substring) pair; the dashboard
    renders one tile per tracker showing that author's most recent
    messages in this inbox. With no trackers configured, the section
    is hidden entirely.
    """


@admin_inbox_trackers_group.command("show")
@click.argument("name")
def admin_inbox_trackers_show_command(name: str) -> None:
    """Print the tracker dict for one inbox."""
    try:
        inbox = get_inbox(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    authors = inbox.tracked_authors or {}
    if not authors:
        click.echo(f"{inbox.name}: no trackers configured")
        return
    label_w = max(len(label) for label in authors)
    click.echo(f"{inbox.name}: {len(authors)} tracker(s)")
    for label, substring in authors.items():
        click.echo(f"  {label:<{label_w}}  {substring}")


@admin_inbox_trackers_group.command("set")
@click.argument("name")
@click.argument("pairs", nargs=-1, required=True)
def admin_inbox_trackers_set_command(name: str, pairs: tuple[str, ...]) -> None:
    """Replace the tracker dict in one shot. Each PAIR is LABEL=SUBSTRING."""
    authors: dict[str, str] = {}
    for pair in pairs:
        label, substring = _parse_pair(pair)
        authors[label] = substring

    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            inbox_dict = get_broker_client().inbox_set_tracked_authors(
                name,
                authors,
            )
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        n = len(inbox_dict.get("tracked_authors") or {})
        click.echo(f"{inbox_dict['name']}: set {n} tracker(s)")
        return

    try:
        inbox = set_tracked_authors(name, authors)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{inbox.name}: set {len(inbox.tracked_authors or {})} tracker(s)")


@admin_inbox_trackers_group.command("add")
@click.argument("name")
@click.argument("label")
@click.argument("substring")
def admin_inbox_trackers_add_command(
    name: str,
    label: str,
    substring: str,
) -> None:
    """Add (or replace) one tracker entry."""
    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            inbox_dict = get_broker_client().inbox_add_tracked_author(
                name,
                label,
                substring,
            )
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        n = len(inbox_dict.get("tracked_authors") or {})
        click.echo(
            f"{inbox_dict['name']}: added tracker {label!r} → {substring!r} ({n} total)"
        )
        return

    try:
        inbox = add_tracked_author(name, label, substring)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(
        f"{inbox.name}: added tracker {label!r} → {substring!r} "
        f"({len(inbox.tracked_authors or {})} total)"
    )


@admin_inbox_trackers_group.command("remove")
@click.argument("name")
@click.argument("label")
def admin_inbox_trackers_remove_command(name: str, label: str) -> None:
    """Remove one tracker entry by label."""
    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            inbox_dict = get_broker_client().inbox_remove_tracked_author(
                name,
                label,
            )
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        n = len(inbox_dict.get("tracked_authors") or {})
        click.echo(f"{inbox_dict['name']}: removed tracker {label!r} ({n} remaining)")
        return

    try:
        inbox = remove_tracked_author(name, label)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    n = len(inbox.tracked_authors or {})
    click.echo(f"{inbox.name}: removed tracker {label!r} ({n} remaining)")


@admin_inbox_trackers_group.command("clear")
@click.argument("name")
def admin_inbox_trackers_clear_command(name: str) -> None:
    """Drop all tracker entries (writes NULL)."""
    if _broker_dispatch_enabled():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            inbox_dict = get_broker_client().inbox_clear_tracked_authors(name)
        except BrokerUnavailable as exc:
            raise _inbox_click_error(exc)
        click.echo(f"{inbox_dict['name']}: cleared all trackers")
        return

    try:
        inbox = clear_tracked_authors(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{inbox.name}: cleared all trackers")
