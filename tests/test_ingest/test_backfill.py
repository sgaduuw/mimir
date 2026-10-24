"""Tests for mimir/ingest/backfill.py: `backfill_canonicals`
over already-ingested articles, the `--limit` /
`--inbox-filter` / `--reprocess` flags, the missing-blob
skip path, and the observation tally re-derivation."""

from sqlalchemy import delete, select, update

from mimir.ingest import (
    backfill_canonicals,
    ingest_epoch,
)
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    InboxAddressObservation,
)
from tests.test_ingest._helpers import (
    _alpha,
    _build_pubinbox_repo,
    _clear_seed_articles,
    _ingest_with_to,
    _rfc5322,
)


def _beta(seeded_db) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()


def test_backfill_resolves_canonical_when_to_matches_known_address(
    seeded_db, tmp_path, broker_active
):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf1@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf1@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id is None
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()

    result = backfill_canonicals(promote_every=1)

    assert result.examined == 1
    assert result.resolved == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf1@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == alpha.id


def test_backfill_unresolved_when_no_address_matches(
    seeded_db, tmp_path, broker_active
):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf-noop@example.com",
        to="alice@example.com",
    )

    result = backfill_canonicals(promote_every=1)

    assert result.examined == 1
    assert result.resolved == 0
    assert result.unresolved == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-noop@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id is None


def test_backfill_respects_limit(seeded_db, tmp_path, broker_active):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(f"lim{i}@example.com", to="linux-fsdevel@vger.kernel.org")
        for i in range(5)
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", tmp_path / "0.git", workers=1)

    result = backfill_canonicals(limit=2)
    assert result.examined == 2


def test_backfill_inbox_filter_restricts_walk(seeded_db, tmp_path, broker_active):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf-filter@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )

    result = backfill_canonicals(inbox_filter="beta")
    assert result.examined == 0


def test_backfill_skip_when_blob_missing(seeded_db, tmp_path, broker_active):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf-gone@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path / "vanished")
        s.commit()

    result = backfill_canonicals(promote_every=1)
    assert result.examined == 1
    assert result.skipped == 1
    assert result.resolved == 0


def test_backfill_reprocess_re_examines_already_set(seeded_db, tmp_path, broker_active):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf-rep@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )
    # Cross-post into beta too: a pin can only move to an inbox the
    # article is linked to (#675).
    _ingest_with_to(
        seeded_db,
        tmp_path,
        _beta(seeded_db),
        "bf-rep@example.com",
        to="linux-fsdevel@vger.kernel.org",
        repo_dir="1.git",
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()

    backfill_canonicals(promote_every=1)
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-rep@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == alpha.id

    with seeded_db() as s:
        a = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        a.list_address = None
        b = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        b.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()
        beta_id = b.id

    no_op = backfill_canonicals(promote_every=1)
    assert no_op.examined == 0

    rep = backfill_canonicals(reprocess=True, promote_every=1)
    assert rep.examined == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-rep@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == beta_id


def test_backfill_records_observations(seeded_db, tmp_path, broker_active):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(f"obs{i}@example.com", to="linux-fsdevel@vger.kernel.org")
        for i in range(3)
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        s.execute(delete(InboxAddressObservation))
        s.commit()

    backfill_canonicals(promote_every=10, reprocess=True)

    with seeded_db() as s:
        cnt = s.execute(
            select(InboxAddressObservation.count)
            .where(InboxAddressObservation.inbox_id == alpha.id)
            .where(InboxAddressObservation.address == "linux-fsdevel@vger.kernel.org")
        ).scalar_one()
    assert cnt == 3


# Regression: RFC 5322 dates with `-0000` come back tz-naive from
# email.utils.parsedate_to_datetime. Mixing those into max() with
# tz-aware dates raised TypeError mid-ingest and rolled back the
# whole batch, production lkml ingest crashed after walking 6M
# commits with only 26 articles persisting.


def test_backfill_reprocess_never_pins_an_unlinked_inbox(
    seeded_db, tmp_path, broker_active
):
    """#675: the backfill repairs the stored rows, so it must use the
    same linked-only predicate as ingest. To names beta's list first, Cc
    names alpha's, and the article is filed only in alpha: the re-pick
    must land on alpha, not beta."""
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    alpha_addr = "linux-arm-kernel@lists.infradead.org"
    beta_addr = "linux-kernel@vger.kernel.org"
    with seeded_db() as s:
        s.execute(
            update(Inbox).where(Inbox.name == "alpha").values(list_address=alpha_addr)
        )
        s.execute(
            update(Inbox).where(Inbox.name == "beta").values(list_address=beta_addr)
        )
        s.commit()
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf-unlinked@example.com",
        to=beta_addr,
        cc=alpha_addr,
    )

    backfill_canonicals(reprocess=True, promote_every=1)

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-unlinked@example.com")
        ).scalar_one()
        links = set(
            s.execute(
                select(ArticleList.inbox_id).where(ArticleList.article_id == art.id)
            ).scalars()
        )
        assert links == {alpha.id}
        assert art.canonical_inbox_id == alpha.id


def test_from_scratch_ingest_with_pinned_firehose_needs_the_backfill(
    seeded_db, broker_active, monkeypatch, tmp_path
):
    """#766: with the firehose pinned, a from-scratch ingest links its
    cross-posts before the topical inbox has promoted its own address,
    so they pin to the demoted firehose. The documented follow-up,
    `admin canonicals backfill --reprocess`, settles them on the
    topical list."""
    from mimir.cli._common import _select_inboxes
    from mimir.config import settings
    from mimir.ingest import ingest_all

    topical = "linux-arm-kernel@lists.infradead.org"
    firehose = "linux-kernel@vger.kernel.org"
    # beta is the lkml stand-in: the pinned, demoted firehose.
    monkeypatch.setattr(settings, "pinned_inboxes", ["beta"])
    monkeypatch.setattr(settings, "canonical_demoted_inboxes", ["beta"])

    cross = [_rfc5322(f"x{i}@example.com", to=firehose, cc=topical) for i in range(5)]
    own = {
        "alpha": [_rfc5322(f"a{i}@example.com", to=topical) for i in range(60)],
        "beta": [_rfc5322(f"b{i}@example.com", to=firehose) for i in range(60)],
    }
    with seeded_db() as s:
        for ix in s.execute(select(Inbox)).scalars():
            mirror = tmp_path / ix.name
            mirror.mkdir()
            _build_pubinbox_repo(mirror / "0.git", own.get(ix.name, []) + cross)
            ix.mirror_path = str(mirror)
        s.commit()

    ingest_all(_select_inboxes(None), workers=1)
    backfill_canonicals(reprocess=True)

    with seeded_db() as s:
        names = {i.id: i.name for i in s.execute(select(Inbox)).scalars()}
        held = {i.name: i.list_address for i in s.execute(select(Inbox)).scalars()}
        pin = s.execute(
            select(Article.canonical_inbox_id).where(
                Article.message_id == "x0@example.com"
            )
        ).scalar_one()
    assert held == {"alpha": topical, "beta": firehose}
    assert names.get(pin) == "alpha"
