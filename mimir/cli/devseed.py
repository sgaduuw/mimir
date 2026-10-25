"""`dev-seed-thread`: build a synthetic multi-message thread and
ingest it into the local DB through the broker so the message-page UI
has real-shaped data to render.

Dev-only; not for production. Idempotent if the inbox already exists
with a synthetic mirror, appends more messages to the same epoch
on re-run.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.inboxes import InboxValidationError, validate_name
from mimir.models import Article, ArticleList, Inbox
from mimir.web.urls import _msg_path


@click.command("dev-seed-thread")
@click.option(
    "--inbox",
    "inbox_name",
    default="dev-thread",
    show_default=True,
    help="Inbox name to seed under. Created if missing.",
)
@click.option(
    "--messages",
    "n_messages",
    default=8,
    show_default=True,
    help="Number of messages in the thread (root + replies).",
)
@click.option(
    "--mirror-root",
    "mirror_root",
    default="Inboxes",
    show_default=True,
    help="Parent directory for the synthetic mirror. Gitignored.",
)
def dev_seed_thread_command(
    inbox_name: str,
    n_messages: int,
    mirror_root: str,
) -> None:
    """Build a synthetic multi-message thread and ingest it.

    Needs a running broker, which does every DB write. Use during
    local dev to populate an empty DB so the message-page UI (thread
    tree, fold states, HTMX swap) has real data to render.
    Not for production. The repo lives at
    `<mirror_root>/<inbox>/git/0.git` and is gitignored via the
    existing `Inboxes/` rule.

    Idempotent if the inbox already exists with a synthetic mirror;
    appends more messages to the same epoch on re-run.

    Example, with the broker running (README, "Try it locally"):

        uv run mimir dev-seed-thread --messages 12
        uv run mimir run
        # navigate to http://127.0.0.1:5000/dev-thread/
    """
    # Validate the inbox name before it gets splatted into both the
    # filesystem path (mirror_dir below) and the RFC 5322 To: header
    # bytes for each synthesised message. The same slug regex the
    # admin service layer uses catches `..`, slashes, CR/LF (which
    # would inject a second header line), and shell metacharacters
    # in one shot.
    try:
        inbox_name = validate_name(inbox_name)
    except InboxValidationError as exc:
        raise click.BadParameter(str(exc), param_hint="--inbox")

    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import (
        InboxCreateRequest,
        InboxUpdateRequest,
        IngestInboxRequest,
    )
    from mimir.inboxes import InboxNotFound, get_inbox
    from mimir.ingest.epoch import IngestResult

    # This process opens SQLite query_only, so every DB write is an
    # existing broker request, the ones `admin inbox add`/`update` and
    # `ingest` send. The mirror is written here and the broker reads it
    # from disk, so both must run on one host.
    def _call(req) -> dict:
        try:
            return get_broker_client().call(req)
        except BrokerUnavailable as exc:
            raise click.ClickException(f"broker {exc}") from exc

    # Absolute, because the broker and the web process each resolve a
    # relative mirror_path against their own working directory.
    mirror_dir = Path(mirror_root).absolute() / inbox_name / "git"
    epoch_dir = mirror_dir / "0.git"
    mirror_dir.mkdir(parents=True, exist_ok=True)

    try:
        ix = get_inbox(inbox_name)
        click.echo(f"using existing inbox '{inbox_name}'")
        inbox_id = ix.id
        if ix.mirror_path != str(mirror_dir):
            _call(InboxUpdateRequest(name=inbox_name, mirror_path=str(mirror_dir)))
    except InboxNotFound:
        # Validators require https://. NOT never-fetched: `mimir update`
        # with no --inbox selects every row and calls `sync_epochs` on
        # each, so this host IS resolved (and fails, counted as `sync
        # failed`) in dev. Harmless, but it is a claim about caller
        # behaviour, not a property of the value. Never fetched for a
        # dev-seed inbox, so the host is a placeholder. Narrow except
        # to InboxNotFound only so a real DB error (connection lost,
        # schema mismatch) surfaces as a traceback rather than
        # silently triggering an unwanted create.
        created = _call(
            InboxCreateRequest(
                name=inbox_name,
                mirror_path=str(mirror_dir),
                upstream_url=f"https://local-dev.invalid/{inbox_name}",
            )
        )
        inbox_id = created["inbox"]["id"]
        click.echo(f"created inbox '{inbox_name}'")

    if epoch_dir.exists():
        repo = Repo(str(epoch_dir))
    else:
        repo = Repo.init_bare(str(epoch_dir), mkdir=True)

    # Build a thread: root + n_messages-1 replies. Half flat-under-root,
    # half nested (each replying to the previous), so the tree exercises
    # both wide and deep shapes. Spread dates 1 hour apart.
    start = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
    parent_msgids: list[str] = []
    prev_commit = None

    def _commit(blob: Blob) -> bytes:
        nonlocal prev_commit
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        c = Commit()
        c.tree = tree.id
        c.parents = [prev_commit] if prev_commit else []
        c.author = c.committer = b"dev-seed <seed@example.invalid>"
        ts = int(start.timestamp()) + len(parent_msgids) * 3600
        c.commit_time = c.author_time = ts
        c.commit_timezone = c.author_timezone = 0
        c.encoding = b"UTF-8"
        c.message = f"add msg {len(parent_msgids)}".encode()
        repo.object_store.add_object(c)
        prev_commit = c.id
        return c.id

    # Per-invocation uniqifier so re-running within the same second
    # still produces fresh message-ids. Microsecond precision is enough
    # to make collisions essentially impossible in dev use.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    for i in range(n_messages):
        is_root = i == 0
        if is_root:
            msgid = f"dev-thread-root-{stamp}@example.invalid"
            subject = f"[seed] root of a {n_messages}-message thread"
            in_reply_to = None
        else:
            msgid = f"dev-thread-reply-{i}-{stamp}@example.invalid"
            subject = f"Re: [seed] root of a {n_messages}-message thread"
            # Alternate flat replies and nested: even = reply to root, odd = reply to prev.
            in_reply_to = parent_msgids[0] if i % 2 == 0 else parent_msgids[-1]
        author = f"Dev Seed {i} <seed{i}@example.invalid>"
        date_str = (start + timedelta(hours=i)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        extra = b""
        if in_reply_to:
            extra += b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n"
            extra += b"References: <" + in_reply_to.encode() + b">\r\n"
        raw = (
            b"Message-ID: <" + msgid.encode() + b">\r\n"
            b"From: " + author.encode() + b"\r\n"
            b"To: <" + inbox_name.encode() + b"@lists.example.invalid>\r\n"
            b"Subject: " + subject.encode() + b"\r\n"
            b"Date: " + date_str.encode() + b"\r\n" + extra + b"\r\n"
            b"This is seed message "
            + str(i).encode()
            + b" of the dev-seed thread. Click around the UI from here."
        )
        _commit(Blob.from_string(raw))
        parent_msgids.append(msgid)

    repo.refs[b"HEAD"] = prev_commit

    raw = _call(IngestInboxRequest(inbox_name=inbox_name, workers=1))["results"]
    for result in (IngestResult.model_validate(r) for r in raw):
        click.echo(
            f"ingested into '{inbox_name}': "
            f"new={result.new} linked={result.linked} "
            f"dup_batch={result.dup_batch} dup_db={result.dup_db} "
            f"failed={result.failed}"
        )

    # Print useful URLs.
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == inbox_id)).scalar_one()
        latest = s.execute(
            select(Article)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == ix.id)
            .order_by(Article.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest and latest.date:
            url = _msg_path(latest.id, latest.date, inbox_name)
            click.echo(f"navigate to: http://127.0.0.1:5000{url}")
            click.echo(f"  (inbox dashboard: http://127.0.0.1:5000/{inbox_name}/)")
