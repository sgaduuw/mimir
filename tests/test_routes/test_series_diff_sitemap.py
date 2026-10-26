"""Tests for the series diff sitemap (#846): `/sitemap-series-diffs-<h>.xml`,
one urlset per first hex character of the series key, listing the
cover-letter diff between each pair of adjacent revisions."""

import html as html_lib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from tests.test_routes._helpers import _clear_sitemap_cache
from tests.test_routes.test_series_diff import (
    _canonical,
    _cross_list_series,
    _put_series_under_head,
)

_URL_RE = re.compile(r"<url><loc>([^<]+)</loc>(?:<lastmod>([^<]+)</lastmod>)?</url>")


def _seed_covers(key, covers):
    """One article per `(version, date[, inbox[, position]])` in `covers`,
    ids in list order, no mirror (the sitemap reads the index only). The
    inbox defaults to alpha and the position to 0, a cover letter."""
    with SessionLocal() as s:
        inbox_ids = {i.name: i.id for i in s.execute(select(Inbox)).scalars()}
        for i, (version, date, *rest) in enumerate(covers):
            inbox = rest[0] if rest else "alpha"
            position = rest[1] if len(rest) > 1 else 0
            s.add(
                Article(
                    message_id=f"{key[:8]}-{i}@x",
                    subject=f"[PATCH {version or ''} {position}/1] s",
                    author="Alice <a@example>",
                    date=date,
                    subject_normalized="s",
                    patch_series_key=key,
                    patch_series_version=version,
                    patch_series_position=position,
                    lists=[
                        ArticleList(
                            inbox_id=inbox_ids[inbox],
                            epoch="0.git",
                            commit_sha="aa" * 20,
                        )
                    ],
                )
            )
            s.flush()
        s.commit()


def _entries(client, bucket):
    """`{(key, from, to): lastmod}` for every URL in one page."""
    _clear_sitemap_cache()
    r = client.get(f"/sitemap-series-diffs-{bucket}.xml")
    assert r.status_code == 200
    xml = r.get_data(as_text=True)
    out = {}
    for loc, lastmod in _URL_RE.findall(xml):
        q = parse_qs(urlparse(html_lib.unescape(loc)).query)
        key = urlparse(html_lib.unescape(loc)).path.split("/")[3]
        out[(key, q["from"][0], q["to"][0])] = lastmod or None
    return out


def _d(month, day=1):
    return datetime(2024, month, day, tzinfo=UTC)


def test_only_adjacent_cover_diffs_are_listed(client):
    """v1, v2, v3 give v1->v2 and v2->v3, never v1->v3. A resend is the
    same revision, not a step. A revision with no cover letter is absent
    from the fold's cover row, so no pair names it."""
    three = "a" + "1" * 39
    gap = "a" + "2" * 39
    _seed_covers(three, [("v1", _d(1)), ("v2", _d(2)), ("v2", _d(3)), ("v3", _d(4))])
    _seed_covers(gap, [("v1", _d(1)), ("v2", _d(2), "alpha", 1), ("v3", _d(4))])

    pairs = set(_entries(client, "a"))
    assert pairs == {
        (three, "v1", "v2"),
        (three, "v2", "v3"),
        (gap, "v1", "v3"),
    }


def test_an_unversioned_resend_folds_into_the_revision_it_resends(client):
    """`[PATCH RESEND 0/3]` with no vN is stored as version "resend" in the
    series of the v1 it resends. It is a copy of a revision, not a step, so
    the pair is v1->v2, not v1->resend and resend->v2."""
    key = "9" * 40
    _seed_covers(key, [("v1", _d(1)), ("resend", _d(1, 20)), ("v2", _d(3))])
    assert set(_entries(client, "9")) == {(key, "v1", "v2")}


def test_a_null_version_cover_is_never_listed(client):
    """The route 404s a version it cannot match, and `to=None` is one."""
    key = "e" * 40
    _seed_covers(key, [("v1", _d(1)), (None, _d(1, 15)), ("v2", _d(2))])
    assert set(_entries(client, "e")) == {(key, "v1", "v2")}


def test_an_undated_cover_sorts_last(client):
    """The fold orders by date ascending, nulls last: an undated v1 comes
    after a dated v2, so the pair is v2->v1."""
    key = "f" * 40
    _seed_covers(key, [("v1", None), ("v2", _d(2))])
    assert set(_entries(client, "f")) == {(key, "v2", "v1")}


def test_the_lowest_id_cover_in_the_to_slot_picks_the_inbox(client):
    """As on the page: v2 lands first in beta, its resend (higher id) in
    alpha, so the URL is under beta."""
    key = "d" * 40
    _seed_covers(key, [("v1", _d(1)), ("v2", _d(2), "beta"), ("v2", _d(3), "alpha")])
    _clear_sitemap_cache()
    xml = client.get("/sitemap-series-diffs-d.xml").get_data(as_text=True)
    (loc,) = [html_lib.unescape(loc) for loc, _ in _URL_RE.findall(xml)]
    assert f"/beta/series/{key}/diff?" in loc, loc


def test_lastmod_is_the_newest_date_across_both_slots(client):
    """A slot is every cover at that version, resends included, and the
    newest of either slot dates the diff. The v2 resend, sent after v3,
    dates v1->v2 from the `to` slot and v2->v3 from the `from` slot."""
    key = "b" * 40
    _seed_covers(key, [("v1", _d(1)), ("v2", _d(2)), ("v3", _d(6)), ("v2", _d(7, 3))])
    assert _entries(client, "b") == {
        (key, "v1", "v2"): "2024-07-03",
        (key, "v2", "v3"): "2024-07-03",
    }
    key2 = "b" + "0" * 39
    _seed_covers(key2, [("v1", _d(1)), ("v2", _d(2)), ("v3", _d(4, 20))])
    entries = _entries(client, "b")
    assert entries[(key2, "v1", "v2")] == "2024-02-01"
    assert entries[(key2, "v2", "v3")] == "2024-04-20"


def test_a_key_is_listed_only_in_its_first_hex_characters_page(client):
    _seed_covers("c" * 40, [("v1", _d(1)), ("v2", _d(2))])
    assert set(_entries(client, "c")) == {("c" * 40, "v1", "v2")}
    for other in "0123456789abdef":
        assert _entries(client, other) == {}, other


@pytest.mark.parametrize("bucket", ["g", "A", "00", "", "-"])
def test_a_bucket_outside_lowercase_hex_is_a_404(client, bucket):
    assert client.get(f"/sitemap-series-diffs-{bucket}.xml").status_code == 404


def test_the_sitemap_index_lists_all_sixteen_pages(client):
    _clear_sitemap_cache()
    xml = client.get("/sitemap.xml").get_data(as_text=True)
    pages = re.findall(
        r"<loc>http://localhost/sitemap-series-diffs-(.)\.xml</loc>", xml
    )
    assert pages == list("0123456789abcdef")


@pytest.mark.parametrize("split", [False, True], ids=["key_only", "split_off"])
def test_every_loc_is_byte_identical_to_its_pages_own_canonical(
    client, tmp_path, split
):
    """Producer and validator: the sitemap spells each URL with the
    helper the page uses for its canonical. v2 is filed in alpha and
    beta, so the inbox is a real choice, and `split` puts the series
    under a head, so `s=` is spelled too."""
    key = _cross_list_series(tmp_path)
    head = _put_series_under_head(lambda v1_id: v1_id if split else None)

    _clear_sitemap_cache()
    xml = client.get(f"/sitemap-series-diffs-{key[0]}.xml").get_data(as_text=True)
    locs = [html_lib.unescape(loc) for loc, _ in _URL_RE.findall(xml)]
    mine = [loc for loc in locs if f"/series/{key}/" in loc]
    assert len(mine) == 1, locs  # precondition: the fixture's one diff
    assert ("&s=" in mine[0]) == split, mine[0]
    assert split is False or mine[0].endswith(f"&s={head}")

    checked = 0
    for loc in locs:
        page = client.get(loc.replace("http://localhost", ""))
        assert page.status_code == 200, loc
        assert _canonical(page.get_data(as_text=True)) == loc
        checked += 1
    assert checked == len(locs) >= 1


def test_the_bucket_query_seeks_the_series_key_index(seeded_db):
    """AGENTS.md check 6: the SQL the builder runs, under production
    statistics. A bucket is a range on `ix_articles_patch_series_key`;
    without it this reads every article."""
    from mimir.seo.sitemaps import series_diff_sitemap_xml
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        plan = plan_of(
            s,
            lambda: series_diff_sitemap_xml(s, "a", "", force=True),
            "patch_series_version",
        )
    assert (
        "ix_articles_patch_series_key (patch_series_key>? AND patch_series_key<?)"
        in plan
    ), plan
