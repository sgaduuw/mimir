"""Tests for mimir/ingest/backfill.py: `backfill_canonicals`
over already-ingested articles, the `--limit` /
`--inbox-filter` / `--reprocess` flags, the missing-blob
skip path, and the observation tally re-derivation."""

from sqlalchemy import delete, select

from mimir.ingest import (
    backfill_canonicals,
    ingest_epoch,
)
from mimir.models import (
    Article,
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


def test_backfill_resolves_canonical_when_to_matches_known_address(seeded_db, tmp_path):
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


def test_backfill_unresolved_when_no_address_matches(seeded_db, tmp_path):
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


def test_backfill_respects_limit(seeded_db, tmp_path):
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


def test_backfill_inbox_filter_restricts_walk(seeded_db, tmp_path):
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


def test_backfill_skip_when_blob_missing(seeded_db, tmp_path):
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


def test_backfill_reprocess_re_examines_already_set(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db,
        tmp_path,
        alpha,
        "bf-rep@example.com",
        to="linux-fsdevel@vger.kernel.org",
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


def test_backfill_records_observations(seeded_db, tmp_path):
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
