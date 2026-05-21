"""`show`: fetch and pretty-print one article by Message-ID.

Threading-debug surface that pairs the DB-side row state (linked
inboxes, indexed date, thread_parent) with the freshly re-parsed
blob (full headers, body, attachments).
"""
import click
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from mimir.extensions import SessionLocal
from mimir.inboxes import bootstrap_inboxes
from mimir.models import Article, ArticleList
from mimir.store import MessageNotFound, read_message


@click.command("show")
@click.argument("message_id")
@click.option(
    "--inbox",
    "inbox_filter",
    type=str,
    default=None,
    help="Read the blob from this inbox's mirror. Default: first linked inbox.",
)
@click.option("--body-chars", type=int, default=2000, help="Truncate body output (-1 for full).")
@click.option("--no-body", is_flag=True, help="Skip the body; useful for inspecting threading state alone.")
def show_command(
    message_id: str,
    inbox_filter: str | None,
    body_chars: int,
    no_body: bool,
) -> None:
    """Fetch and pretty-print one article by Message-ID.

    Shows DB-side fields (inboxes it's linked to, indexed date, thread_parent
    and whether it's in the archive) alongside the freshly re-parsed blob
    (full headers, body, attachments). Designed for threading debug.
    """
    bootstrap_inboxes()
    with SessionLocal() as session:
        article = session.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one_or_none()
        if article is None:
            raise click.ClickException(f"no article with message_id={message_id!r}")

        links = session.execute(
            select(ArticleList)
            .where(ArticleList.article_id == article.id)
            .options(selectinload(ArticleList.inbox))
        ).scalars().all()
        if not links:
            raise click.ClickException(f"article {message_id!r} has no inbox links")

        if inbox_filter is not None:
            chosen = next(
                (link for link in links if link.inbox.name == inbox_filter), None
            )
            if chosen is None:
                raise click.ClickException(
                    f"article not linked to inbox {inbox_filter!r}; "
                    f"linked to: {[link.inbox.name for link in links]}"
                )
        else:
            chosen = links[0]

        try:
            parsed = read_message(session, chosen.inbox, message_id)
        except MessageNotFound as exc:
            raise click.ClickException(str(exc))

        # Resolve threading state: where does our parent point, and is it in DB?
        parent_present = None
        if article.thread_parent:
            parent_present = session.execute(
                select(Article.id).where(Article.message_id == article.thread_parent)
            ).scalar_one_or_none() is not None

    click.echo("--- DB row ---")
    click.echo(f"id:            {article.id}")
    click.echo(
        f"linked inboxes:{', '.join(f'{link.inbox.name}/{link.epoch}@{link.commit_sha[:10]}' for link in links)}"
    )
    click.echo(f"reading from:  {chosen.inbox.name}")
    click.echo(f"date:          {article.date.isoformat() if article.date else ''}")
    click.echo(f"thread_parent: {article.thread_parent or '(none)'}"
               + (f"  [in DB: {parent_present}]" if article.thread_parent else ""))
    click.echo()
    click.echo("--- parsed blob ---")
    click.echo(f"Message-ID: {parsed.message_id}")
    click.echo(f"From:       {parsed.author or ''}")
    click.echo(f"Date:       {parsed.date.isoformat() if parsed.date else ''}")
    click.echo(f"Subject:    {parsed.subject or ''}")
    if parsed.in_reply_to:
        click.echo(f"In-Reply-To: {parsed.in_reply_to}")
    if parsed.references:
        click.echo(f"References: {' '.join(parsed.references)}")
    for a in parsed.attachments:
        click.echo(f"Attachment: {a.filename or '(no name)'} [{a.content_type}] {len(a.content)} bytes")
    if no_body:
        return
    click.echo()
    if parsed.body:
        body = parsed.body if body_chars < 0 else parsed.body[:body_chars]
        click.echo(body)
        if body_chars >= 0 and len(parsed.body) > body_chars:
            click.echo(f"\n... ({len(parsed.body) - body_chars} more chars truncated; pass --body-chars=-1 for full)")
