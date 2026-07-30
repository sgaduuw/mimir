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
not a free `ADD COLUMN`. SQLite has no `ADD CONSTRAINT`, so alembic's
batch mode cannot take the native `ALTER TABLE` path and must move-and-
copy: `CREATE TABLE _alembic_tmp_article_lists`, `INSERT ... SELECT`,
`DROP TABLE article_lists`, rename, then rebuild the indexes.

Cost, measured 2026-07-30 against a synthetic corpus matching
production's shape (34.09M `article_lists` rows, 203 inboxes, UNIQUE
`message_id` on 17.34M articles), with the same pragmas including
`foreign_keys=ON`:

    FK-enforced INSERT ... SELECT   55.4 s
    DROP TABLE                      10.1 s
    two index builds                34.4 s
    COMMIT / WAL checkpoint         28.3 s
    -------------------------------------
    total                          130   s   (~110 s scaled to prod)

An earlier version of this docstring said "~8 s on a seeded 6M-row
corpus, so budget ~5x". That was wrong twice, and both are the standard
traps. It counted two statements where there are five, and it assumed
linear scaling: 5.7x the rows cost 12.5x the time, because the index
builds sort and the WAL checkpoint is superlinear in dirty pages. Any
budget derived from a row count needs measuring at that row count.

That is writer hold at broker startup before the sentinel flips, which
is the right place to pay it, but it is not instant and should not be
described as such.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1072ad1fae96'
down_revision: Union[str, Sequence[str], None] = 'e3aa78c72a8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_batch_scratch_table() -> None:
    """Clear debris from a previous interrupted attempt, but ONLY when
    the live table still exists.

    Alembic runs SQLite DDL NON-transactionally (it says so in its own
    log: "Will assume non-transactional DDL"), so there is no rollback
    to undo a half-finished move-and-copy. If the `INSERT ... SELECT`
    raises, or the process is killed mid-rebuild,
    `_alembic_tmp_article_lists` survives, and every subsequent attempt
    dies immediately with `table _alembic_tmp_article_lists already
    exists`.

    That turns a transient failure into a permanent one, and the
    deployment shape makes it worse: the broker runs this at startup
    under systemd with `Restart=always` and `RestartUSec=100ms`, so a
    single SIGTERM landing inside the rebuild leaves the container
    crash-looping on debris it cannot clear itself, with `Requires=`
    holding the web tier down behind it.

    The `article_lists` check is NOT defensive padding, and an earlier
    version of this function omitted it and was a data-loss bug. Batch
    mode's order is:

        CREATE TABLE _alembic_tmp_article_lists
        INSERT INTO _alembic_tmp_article_lists SELECT ... FROM article_lists
        DROP TABLE article_lists          <-- (b) begins here
        ALTER TABLE _alembic_tmp_article_lists RENAME TO article_lists

    so there are two distinct interrupted states, and they need opposite
    handling:

      (a) killed during the copy. Both tables exist, the scratch one is
          a partial duplicate, dropping it is right.
      (b) killed between the DROP and the RENAME. The scratch table is
          the ONLY copy of the data. Dropping it destroys the table
          outright; reproduced on 2026-07-30, 0 surviving rows and the
          migration then failing with `NoSuchTableError: article_lists`.

    Window (a) is ~55 s of the ~130 s rebuild and (b) is a metadata
    rename, so (a) is overwhelmingly the likely kill point, which is
    exactly why (b) is easy to reason past. Both are reachable, and only
    one of them is survivable if this guesses wrong.

    State (b) therefore raises rather than auto-repairing. Completing the
    rename here would also have to recreate the indexes the rebuild had
    not reached yet and then reconcile `alembic_version`, and a migration
    that silently reshapes a 28.8M-row table on an assumption about how
    it died is not a trade worth making. The error carries the exact
    recovery, which is deterministic.
    """
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "_alembic_tmp_article_lists" not in tables:
        return
    if "article_lists" not in tables:
        raise RuntimeError(
            "_alembic_tmp_article_lists exists but article_lists does not. "
            "A previous run was interrupted between alembic's DROP and its "
            "RENAME, so the scratch table is the ONLY copy of the data and "
            "must not be dropped. Recover by hand, then restart:\n"
            "  ALTER TABLE _alembic_tmp_article_lists RENAME TO article_lists;\n"
            "  CREATE INDEX ix_article_lists_inbox_id\n"
            "    ON article_lists (inbox_id);\n"
            "  CREATE INDEX ix_article_lists_thread_root\n"
            "    ON article_lists (inbox_id, thread_root_id);\n"
            "Then mark this revision applied: alembic stamp 1072ad1fae96\n"
            "(the renamed table already carries thread_root_id and its FK, "
            "so re-running the migration would fail on a duplicate column)."
        )
    op.execute("DROP TABLE _alembic_tmp_article_lists")


def upgrade() -> None:
    _drop_batch_scratch_table()
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
    _drop_batch_scratch_table()
    op.drop_index("ix_article_lists_thread_root", table_name="article_lists")
    with op.batch_alter_table("article_lists") as batch:
        batch.drop_column("thread_root_id")
