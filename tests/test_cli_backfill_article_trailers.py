"""CLI + service test for `backfill-article-trailers`. The
extractor itself is exercised in `tests/test_trailers.py`; ingest-
time trailer extraction is covered by `tests/test_ingest.py`. This
file pins the backfill walker's idempotence + bucket counters."""

from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli.backfill import backfill_article_trailers_command
from mimir.ingest import ingest_epoch
from mimir.models import Article, ArticleTrailer, Inbox
from mimir.trailers import backfill_article_trailers

# Shared helper from tests/test_ingest/_helpers.py — same pattern as
# every other test that builds a synthetic public-inbox repo.
from tests.test_ingest._helpers import _build_pubinbox_repo

_TRAILER_BODY = (
    b"Looks good.\n\n"
    b"Reviewed-by: Alice <Alice@Example.COM>\n"
    b"Acked-by: Bob <bob@kernel.org>\n"
)
_PROSE_BODY = b"hello, no trailers here\n"
_EQUALS_BODY = b"Reviewed-by: X <x=y@example.com>\n"


def _rfc5322(msgid: str, body: bytes) -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: t\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\n" + body
    )


def _ingest_articles_without_trailers(seeded_db, tmp_path, *bodies):
    """Ingest via the real pipeline, then DELETE the ArticleTrailer
    rows so backfill has work."""
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.expunge(alpha)
    epoch_path = tmp_path / "0.git"
    _build_pubinbox_repo(
        epoch_path,
        [_rfc5322(f"m{i}@example.com", body) for i, body in enumerate(bodies)],
    )
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, alpha, "0.git", epoch_path, workers=1)
        s.query(ArticleTrailer).delete()
        s.commit()


def test_backfill_indexes_trailer_articles(seeded_db, tmp_path, broker_active):
    """Pre-extractor articles get re-walked: bodies with attestation
    trailers land ArticleTrailer rows; prose bodies don't.

    `--limit=2` scopes to the freshly-ingested articles (newest-
    first walk; the seeded conftest articles have lower IDs and
    bogus mirror SHAs so they otherwise dominate the `skipped`
    bucket)."""
    _ingest_articles_without_trailers(
        seeded_db,
        tmp_path,
        _TRAILER_BODY,
        _PROSE_BODY,
    )

    result = backfill_article_trailers(limit=2)
    assert result.examined == 2
    assert result.indexed == 1
    assert result.no_trailers == 1
    assert result.failed == 0

    with seeded_db() as s:
        rows = sorted(
            (a.message_id, t.role, t.address, t.address_normalized)
            for a, t in s.execute(
                select(Article, ArticleTrailer).join(
                    ArticleTrailer, ArticleTrailer.article_id == Article.id
                )
            ).all()
        )
    assert rows == [
        ("m0@example.com", "Acked-by", "bob@kernel.org", "bob@kernel.org"),
        ("m0@example.com", "Reviewed-by", "Alice@Example.COM", "alice@example.com"),
    ]


def test_backfill_is_idempotent_on_rerun(seeded_db, tmp_path, broker_active):
    _ingest_articles_without_trailers(seeded_db, tmp_path, _TRAILER_BODY)
    backfill_article_trailers(limit=1)
    second = backfill_article_trailers(limit=1)
    assert second.examined == 1
    assert second.skipped == 1
    assert second.indexed == 0

    with seeded_db() as s:
        roles = sorted(r.role for r in s.execute(select(ArticleTrailer)).scalars())
    assert roles == ["Acked-by", "Reviewed-by"]


def test_backfill_reprocess_re_extracts(seeded_db, tmp_path, broker_active):
    _ingest_articles_without_trailers(seeded_db, tmp_path, _TRAILER_BODY)
    backfill_article_trailers(limit=1)
    result = backfill_article_trailers(limit=1, reprocess=True)
    assert result.examined == 1
    assert result.indexed == 1
    assert result.skipped == 0


def test_backfill_cli_prints_summary(seeded_db, tmp_path, broker_active):
    _ingest_articles_without_trailers(
        seeded_db,
        tmp_path,
        _TRAILER_BODY,
        _PROSE_BODY,
    )
    result = CliRunner().invoke(
        backfill_article_trailers_command,
        ["--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    assert "examined=2" in result.output
    assert "indexed=1" in result.output
    assert "no_trailers=1" in result.output


def test_backfill_skips_articles_with_unreachable_mirror(seeded_db):
    result = backfill_article_trailers()
    assert result.failed == 0
    assert result.skipped > 0


def test_backfill_reads_article_whose_canonical_inbox_is_not_filed(
    seeded_db, tmp_path, broker_active
):
    """#662: `canonical_inbox_id` can name an inbox the article is not
    filed in. The body must be read from a filed inbox instead of the
    article being skipped on every run."""
    from mimir.models import ArticleList

    _ingest_articles_without_trailers(seeded_db, tmp_path, _TRAILER_BODY)
    with seeded_db() as s:
        gamma = Inbox(
            name="gamma", mirror_path="/nonexistent", upstream_url="https://x/g"
        )
        s.add(gamma)
        s.flush()
        art = s.execute(
            select(Article).where(Article.message_id == "m0@example.com")
        ).scalar_one()
        art.canonical_inbox_id = gamma.id
        s.commit()
        # Precondition: gamma names the article but holds no row for it.
        filed = {
            al.inbox_id
            for al in s.execute(
                select(ArticleList).where(ArticleList.article_id == art.id)
            ).scalars()
        }
        assert filed and gamma.id not in filed

    result = backfill_article_trailers(limit=1)
    assert result.examined == 1
    assert result.skipped == 0
    assert result.indexed == 1
    with seeded_db() as s:
        rows = {
            (a.message_id, t.role, t.address_normalized)
            for a, t in s.execute(
                select(Article, ArticleTrailer).join(
                    ArticleTrailer, ArticleTrailer.article_id == Article.id
                )
            ).all()
        }
    assert rows == {
        ("m0@example.com", "Acked-by", "bob@kernel.org"),
        ("m0@example.com", "Reviewed-by", "alice@example.com"),
    }


def _store_trailer(seeded_db, name: str, address: str) -> None:
    """Store a row as the parser before #913 did, on article m0."""
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "m0@example.com")
        ).scalar_one()
        s.add(
            ArticleTrailer(
                article_id=art.id,
                role="Reviewed-by",
                name=name,
                address=address,
                address_normalized=address.lower(),
            )
        )
        s.commit()


def _trailer_rows(seeded_db) -> list[tuple[str, str, str]]:
    with seeded_db() as s:
        return [
            (t.name, t.address, t.address_normalized)
            for t in s.execute(select(ArticleTrailer)).scalars()
        ]


def test_backfill_reprocess_resplits_a_trailer_stored_before_913(
    seeded_db, tmp_path, broker_active
):
    _ingest_articles_without_trailers(seeded_db, tmp_path, _EQUALS_BODY)
    _store_trailer(seeded_db, "X <x=", "y@example.com")
    assert _trailer_rows(seeded_db) == [("X <x=", "y@example.com", "y@example.com")]

    result = backfill_article_trailers(limit=1, reprocess=True)
    assert result.indexed == 1
    assert _trailer_rows(seeded_db) == [("X", "x=y@example.com", "x=y@example.com")]


def test_backfill_reprocess_drops_rows_the_body_no_longer_yields(
    seeded_db, tmp_path, broker_active
):
    """A row whose line the parser now skips (here `A <x"y@...>`, once
    split as `A <x"`) must not outlive `--reprocess` just because the
    article has no other trailer."""
    _ingest_articles_without_trailers(seeded_db, tmp_path, _PROSE_BODY)
    _store_trailer(seeded_db, 'A <x"', "y@example.com")
    assert len(_trailer_rows(seeded_db)) == 1

    result = backfill_article_trailers(limit=1, reprocess=True)
    assert result.examined == 1
    assert result.no_trailers == 1
    assert _trailer_rows(seeded_db) == []
