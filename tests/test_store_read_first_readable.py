"""`mimir.store.read_first_readable`: which inbox and which article a
body is read from, independent of database row order (#661, #662)."""

from sqlalchemy import select

from mimir import store
from mimir.extensions import SessionLocal
from mimir.models import Article, Inbox
from mimir.store import MessageNotFound
from tests.test_routes._helpers import _seed_resends


def test_read_first_readable_picks_lowest_article_id(monkeypatch):
    """Resends share a slot; the pick must not depend on the order the
    caller (or the database) hands them over."""

    monkeypatch.setattr(store, "read_message", lambda s, ix, mid: mid)
    _seed_resends([["alpha"], ["alpha"], ["alpha"]])
    with SessionLocal() as s:
        arts = list(
            s.execute(
                select(Article).where(Article.message_id.like("resend-%"))
            ).scalars()
        )
        assert len(arts) == 3  # precondition
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        newest_first = sorted(arts, key=lambda a: a.id, reverse=True)
        assert newest_first[0].id > newest_first[-1].id  # precondition: reversed
        article, _ = store.read_first_readable(s, newest_first, alpha.id)
        assert article.id == min(a.id for a in arts)


def test_read_first_readable_inbox_order(monkeypatch):
    """The URL's inbox first, then the rest by name. The article's
    `lists` come back in inbox-id order, and `aardvark` is created last
    so that order (alpha, beta, aardvark) differs from name order."""

    asked = []

    def _always_missing(session, inbox, message_id):
        asked.append(inbox.name)
        raise MessageNotFound(message_id)

    monkeypatch.setattr(store, "read_message", _always_missing)
    _seed_resends([["alpha", "beta", "aardvark"]])
    with SessionLocal() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "resend-0@x")
        ).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        zeta = Inbox(
            name="zeta",
            mirror_path="/tmp/zeta",
            upstream_url="https://example.com/zeta",
        )
        s.add(zeta)
        s.flush()
        assert [al.inbox.name for al in art.lists] != [
            "aardvark",
            "alpha",
            "beta",
        ]  # precondition
        assert store.read_first_readable(s, [art], beta.id) is None
        assert asked == ["beta", "aardvark", "alpha"]
        asked.clear()
        # URL inbox not among the article's: pure name order.
        assert store.read_first_readable(s, [art], zeta.id) is None
        assert asked == ["aardvark", "alpha", "beta"]
