"""add robots_rules.content_signals

Revision ID: 82e825291162
Revises: cc79e4a44a23
Create Date: 2026-05-22 19:05:35.009289

Adds a Content-Signal column to robots_rules so operators can express
the search / ai-input / ai-train consent semantics Cloudflare proposed
(https://blog.cloudflare.com/content-signals-policy/). Per-stanza JSON
of `{key: "yes"|"no"}`; renderer emits a `Content-Signal: k=v, k=v`
line between the `User-agent:` line and the `Crawl-delay:`/`Disallow:`
lines.

Backfills the seeded `*` row with the same defaults a fresh deploy
would get (`search=yes, ai-train=no, ai-input=no`): public archive +
existing redaction posture says SEO is the primary use case and AI
training opt-out matches the "friction layer" stance in CONTEXT.md.
Operators can override per-row via `admin robots update '*'
--content-signal …` later.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82e825291162'
down_revision: Union[str, Sequence[str], None] = 'cc79e4a44a23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEFAULT_STAR_CONTENT_SIGNALS = {
    "search": "yes",
    "ai-train": "no",
    "ai-input": "no",
}


def upgrade() -> None:
    op.add_column(
        "robots_rules",
        sa.Column("content_signals", sa.JSON(), nullable=True),
    )
    # Backfill the seeded `*` row only when an operator hasn't already
    # taken ownership of its content_signals (NULL means "untouched").
    # Match-on-NULL avoids clobbering a future operator who edited the
    # row between migrations.
    op.execute(
        sa.text(
            "UPDATE robots_rules SET content_signals = :signals "
            "WHERE user_agent = '*' AND content_signals IS NULL"
        ).bindparams(signals=json.dumps(_DEFAULT_STAR_CONTENT_SIGNALS))
    )


def downgrade() -> None:
    with op.batch_alter_table("robots_rules") as batch:
        batch.drop_column("content_signals")
