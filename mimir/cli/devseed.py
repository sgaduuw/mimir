"""`dev-seed-thread` — build a synthetic multi-message thread and
ingest it into the local DB so the message-page UI has real-shaped
data to render.

Dev-only; not for production. Idempotent if the inbox already exists
with a synthetic mirror, appends more messages to the same epoch
on re-run.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.inboxes import InboxValidationError, validate_name
from mimir.ingest import ingest_epoch
from mimir.models import Article, ArticleList, Inbox


@click.command("dev-seed-thread")
@click.option(
    "--inbox", "inbox_name", default="dev-thread", show_default=True,
    help="Inbox name to seed under. Created if missing.",
)
@click.option(
    "--messages", "n_messages", default=8, show_default=True,
    help="Number of messages in the thread (root + replies).",
)
@click.option(
    "--mirror-root", "mirror_root", default="Inboxes",
    show_default=True,
    help="Parent directory for the synthetic mirror. Gitignored.",
)
def dev_seed_thread_command(
    inbox_name: str, n_messages: int, mirror_root: str,
) -> None:
    """Build a synthetic multi-message thread and ingest it.

    Use during local dev to populate an empty DB so the message-page
    UI (thread tree, fold states, HTMX swap) has real data to render.
    Not for production. The repo lives at
    `<mirror_root>/<inbox>/git/0.git` and is gitignored via the
    existing `Inboxes/` rule.

    Idempotent if the inbox already exists with a synthetic mirror;
    appends more messages to the same epoch on re-run.

    Example:

        poetry run flask --app mimir dev-seed-thread --messages 12
        poetry run flask --app mimir run
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

    from mimir.inboxes import create_inbox, get_inbox

    mirror_dir = Path(mirror_root) / inbox_name / "git"
    epoch_dir = mirror_dir / "0.git"
    mirror_dir.mkdir(parents=True, exist_ok=True)

    try:
        get_inbox(inbox_name)
        click.echo(f"using existing inbox '{inbox_name}'")
    except Exception:
        # Validators require https://; this URL is never fetched for a
        # dev-seed inbox, so the host is a placeholder.
        create_inbox(
            name=inbox_name,
            mirror_path=str(mirror_dir),
            upstream_url=f"https://local-dev.invalid/{inbox_name}",
        )
        click.echo(f"created inbox '{inbox_name}'")

    with SessionLocal() as s:
        # The service-layer create_inbox returned a detached value;
        # reload + pin mirror_path inside this session in case it differs.
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(mirror_dir)
        s.commit()
        inbox_id = ix.id

    if epoch_dir.exists():
        repo = Repo(str(epoch_dir))
    else:
        repo = Repo.init_bare(str(epoch_dir), mkdir=True)

    # Build a thread: root + n_messages-1 replies. Half flat-under-root,
    # half nested (each replying to the previous), so the tree exercises
    # both wide and deep shapes. Spread dates 1 hour apart.
    start = datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc)
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
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
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
            b"Date: " + date_str.encode() + b"\r\n"
            + extra +
            b"\r\n"
            b"This is seed message " + str(i).encode() +
            b" of the dev-seed thread. Click around the UI from here."
        )
        _commit(Blob.from_string(raw))
        parent_msgids.append(msgid)

    repo.refs[b"HEAD"] = prev_commit

    # Ingest into the DB.
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == inbox_id)).scalar_one()
        result = ingest_epoch(s, ix, "0.git", epoch_dir, workers=1)
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
            url = f"/{inbox_name}/{latest.date.year}/{latest.date.month:02d}/{latest.id}"
            click.echo(f"navigate to: http://127.0.0.1:5000{url}")
            click.echo(f"  (inbox dashboard: http://127.0.0.1:5000/{inbox_name}/)")
