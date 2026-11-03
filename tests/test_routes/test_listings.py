"""Tests for the lifecycle-status badges wired into listing routes
(Task 14 of the multi-tree-lifecycle work, updated in Task 12 to
the .badge primitive). The bulk fetcher
`mimir.lifecycle_status.lifecycle_status_for_articles` is called
from every listing route; this module pins that at least one
non-PENDING state produces rendered badges in the listing HTML.

The inbox dashboard's active rows retain the same integration point
while its Recent messages rows are deliberately badge-free (#993).
"""

from datetime import UTC
from html.parser import HTMLParser


def test_active_listing_renders_landed_pill_for_landed_article(client, frozen_clock):
    """A landed active root keeps its badge; the same root in Recent has none."""
    from datetime import datetime, timedelta

    from mimir import cache
    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="f" * 40,
                message_id="art1@example.com",
                tree_name="linus",
                committed_at=datetime.now(UTC),
            )
        )
        s.commit()
        # Conftest's _reset_db wipes the cache table per test, but
        # the seeded MainlineCommit could collide with a pre-existing
        # cache row if test ordering changes; clear explicitly.
        from sqlalchemy import select

        from mimir.models import Article, Inbox

        art = s.execute(
            select(Article).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        art.date = datetime.now(UTC) - timedelta(hours=1)
        s.commit()
        alpha_id = s.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        cache.delete(f"lifecycle_status:{art.id}:i{alpha_id}")

    r = client.get("/alpha/")
    assert r.status_code == 200
    body = r.data.decode()
    # New badge primitive: `_lifecycle_badges.html` emits a
    # `badge-lifecycle-<state>` span when status.state.value != 'pending'.
    assert 'class="badge badge-lifecycle-landed"' in body
    # pill_label for LANDED is the state name uppercased; the template
    # may insert whitespace before the label text but the word appears.
    assert "LANDED" in body
    recent = body.split('id="recent-heading"', 1)[1].split("</section>", 1)[0]
    assert "hello alpha" in recent  # the landed article also appears in Recent
    assert "badge" not in recent
    # A list ordered by recency gives every row the same heat (#988).
    assert "badge-activity-" not in body


def _surfaces(client):
    """Anchor the seed on the frozen day and return the listing URLs
    that are already ordered by recency or activity, one per surface."""
    from datetime import datetime

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, MainlineCommit

    with SessionLocal() as s:
        for mid, when in (
            ("art1@example.com", datetime(2024, 6, 15, 11, 0, tzinfo=UTC)),
            ("art4@example.com", datetime(2024, 6, 15, 11, 30, tzinfo=UTC)),
        ):
            s.scalar(select(Article).where(Article.message_id == mid)).date = when
        s.add(
            MainlineCommit(
                commit_sha="f" * 40,
                message_id="art1@example.com",
                tree_name="linus",
                committed_at=datetime(2024, 6, 15, 11, 45, tzinfo=UTC),
            )
        )
        s.commit()
    return {
        "dashboard": "/alpha/",
        "today": "/alpha/today",
        "since": "/alpha/since/2024-06-14",
        "month": "/alpha/2024/06/",
        "search": "/alpha/search?q=hello",
    }


def test_listings_ordered_by_activity_carry_no_activity_chip(client, frozen_clock):
    """Every row of the dashboard ("Recent messages" and the active
    threads), today, since, month and search is HOT or the same heat:
    the chip says nothing there (#988). The lifecycle chip still
    distinguishes the rows."""

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    for surface, url in _surfaces(client).items():
        body = client.get(url).get_data(as_text=True)
        text = Text()
        text.feed(body)
        assert "hello alpha" in "".join(text.parts), surface
        # Search represents the newer pending reply, not the landed root.
        assert ('class="badge badge-lifecycle-landed"' in body) == (
            surface != "search"
        ), surface
        assert "badge-activity-" not in body, surface


def test_every_dashboard_section_drops_the_activity_chip(client, frozen_clock):
    """Pull requests, release announcements, tracker tiles and "this day
    5 years ago" are recency lists too. The posts are old and unanswered,
    so each would read DORMANT if the chip were shown (#988)."""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    old = datetime(2024, 5, 1, tzinfo=UTC)
    with SessionLocal() as s:
        inbox = s.scalar(select(Inbox).where(Inbox.name == "alpha"))
        inbox.tracked_authors = {"Zed": "zed@example.com"}
        years_ago = datetime.now(UTC) - timedelta(days=365 * 5)
        for mid, subject, when in (
            ("pull@x", "[GIT PULL] fixes for foo", old),
            ("rel@x", "Linux 6.9", old),
            ("track@x", "tracked subject", old),
            ("hist@x", "five years ago", years_ago),
        ):
            art = Article(
                message_id=mid,
                subject=subject,
                author="Zed <zed@example.com>",
                date=when,
                subject_normalized=subject.lower(),
            )
            s.add(art)
            s.flush()
            s.add(
                ArticleList(
                    article_id=art.id,
                    inbox_id=inbox.id,
                    epoch="0.git",
                    commit_sha=f"{len(mid):02d}" * 20,
                    thread_root_id=art.id,
                )
            )
        s.commit()
    body = client.get("/alpha/").get_data(as_text=True)
    for subject in (
        "[GIT PULL] fixes for foo",
        "Linux 6.9",
        "tracked subject",
        "five years ago",
    ):
        assert subject in body, subject
    assert body.count("Latest from Zed") == 1
    assert "badge-activity-" not in body


def test_yesterday_carries_no_activity_chip(client, frozen_clock):
    from datetime import datetime

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _surfaces(client)
    with SessionLocal() as s:
        s.scalar(
            select(Article).where(Article.message_id == "art1@example.com")
        ).date = datetime(2024, 6, 14, 11, 0, tzinfo=UTC)
        s.commit()
    body = client.get("/alpha/yesterday").get_data(as_text=True)
    assert "hello alpha" in body
    assert 'class="badge badge-lifecycle-landed"' in body
    assert "badge-activity-" not in body
