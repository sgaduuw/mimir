"""add article_lists thread_root_id

Revision ID: 1072ad1fae96
Revises: e3aa78c72a8d
Create Date: 2026-07-27 21:59:28.314774

Materialises each article's thread root PER INBOX (W8, see
`_claude/specs/2026-07-27-seo-index-shaping-design.md`).

The column lives on `article_lists`, not on `articles`, and that is the
load-bearing decision. Threading in mimir is inbox-scoped:
`find_thread_root(session, inbox, message_id)` walks only within one
inbox, so the same article genuinely has different roots in different
inboxes. A message cross-posted to lkml and a topical list routinely
hangs off a root present in only one of them, and is its own root in
the other. `article_lists` is already keyed exactly `(article_id,
inbox_id)`, i.e. the per-inbox-presence grain, so it is where a
per-inbox answer belongs. A column on `articles` would be silently
wrong for every cross-post, which is a large fraction of this corpus.

A root points at ITSELF, so "is a root in this inbox" is
`thread_root_id = article_id` with no NULL-means-root ambiguity.

NULL means "not yet computed", not "is a root". That distinction is
what lets this ship without a blocking backfill on ~6M rows: the
column is added instantly, readers fall back to the recursive CTE
wherever it is NULL, and `mimir backfill-thread-roots` fills it in
afterwards while ingest keeps writing.

No data migration here on purpose. Backfilling inside the migration
would hold the writer for the length of a full-corpus walk, on the
single-writer broker, during container startup and before the
healthcheck sentinel is touched.

The FK does force SQLite into a batch-mode table rebuild, so this is
not a free `ADD COLUMN`: measured at ~8 s wall on a seeded 6M-row
corpus (rebuild plus the index). That is writer hold at broker startup
before the sentinel flips, which is the right place to pay it, but it
is not instant and should not be described as such.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1072ad1fae96'
down_revision: Union[str, Sequence[str], None] = 'e3aa78c72a8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The FK is declared, not just the column. Without it
    # `ondelete="SET NULL"` on the model is inert (SQLite enforces
    # only what the schema actually carries, and every connection sets
    # `PRAGMA foreign_keys=ON`), so deleting an article would leave
    # siblings pointing at a nonexistent id. That is reachable:
    # `inboxes.delete_inbox(keep_orphan_articles=False)` deletes orphan
    # articles, and a thread whose root row is deleted would then have
    # NO member satisfying `thread_root_id = article_id`, so it would
    # silently vanish from every "roots in this inbox" query.
    with op.batch_alter_table("article_lists") as batch:
        batch.add_column(sa.Column("thread_root_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_article_lists_thread_root_id_articles",
            "articles",
            ["thread_root_id"],
            ["id"],
            ondelete="SET NULL",
        )
    # Composite and inbox-first because every consumer asks an
    # inbox-scoped question: "the roots in this inbox" (sitemap), "is
    # this article a root here", "how many articles share this root
    # here" (the single-message rule).
    op.create_index(
        "ix_article_lists_thread_root",
        "article_lists",
        ["inbox_id", "thread_root_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_article_lists_thread_root", table_name="article_lists")
    with op.batch_alter_table("article_lists") as batch:
        batch.drop_column("thread_root_id")
