"""Tests for the lifecycle-status badges wired into listing routes
(Task 14 of the multi-tree-lifecycle work, updated in Task 12 to
the .badge primitive). The bulk fetcher
`mimir.lifecycle_status.lifecycle_status_for_articles` is called
from every listing route; this module pins that at least one
non-PENDING state produces rendered badges in the listing HTML.

Picking the per-inbox dashboard's "Recent messages" surface as the
single integration point: it's the lowest-friction listing in the
seeded fixture (alpha already carries `art1@example.com`), and the
badge rendering through `_lifecycle_badges.html` exercises the same
include partial that every other listing template uses.
"""


def test_recent_listing_renders_landed_pill_for_landed_article(client):
    """Seeding a `mainline_commits` row tagged `linus` for one of the
    seeded articles flips its lifecycle state from PENDING to
    LANDED; the per-inbox dashboard's Recent-messages list must
    then render the lifecycle-landed badge on the .badge primitive.

    The fixture `art1@example.com` lives on alpha and appears in
    the dashboard's Recent list. After seeding the landing, the
    bulk fetcher's per-id cache is populated by the page render
    itself; we clear the cache row beforehand so the test isn't
    racing a stale entry from another test."""
    from datetime import datetime, timezone

    from mimir import cache
    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="f" * 40,
                message_id="art1@example.com",
                tree_name="linus",
                committed_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
        # Conftest's _reset_db wipes the cache table per test, but
        # the seeded MainlineCommit could collide with a pre-existing
        # cache row if test ordering changes; clear explicitly.
        from sqlalchemy import select
        from mimir.models import Article

        art = s.execute(
            select(Article).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        cache.delete(f"lifecycle_status:{art.id}")

    r = client.get("/alpha/")
    assert r.status_code == 200
    body = r.data.decode()
    # New badge primitive: `_lifecycle_badges.html` emits a
    # `badge-lifecycle-<state>` span when status.state.value != 'pending'.
    assert 'class="badge badge-lifecycle-landed"' in body
    # pill_label for LANDED is the state name uppercased; the template
    # may insert whitespace before the label text but the word appears.
    assert "LANDED" in body
    # Activity chip always renders alongside the lifecycle badge.
    assert 'class="badge badge-activity-' in body
