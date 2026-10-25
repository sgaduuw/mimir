"""#844: with the `to` cover cross-posted and canonical in another inbox,
the fold link and the sitemap entry still agree, and the link is served."""

import html as htmllib
import re

from sqlalchemy import select

from mimir.models import Article, ArticleList, Inbox
from tests.test_routes._helpers import _clear_sitemap_cache
from tests.test_routes.test_thread import _fold, _seed_cover_series


def test_fold_link_matches_sitemap_for_a_cross_posted_to_cover(client, tmp_path):
    from mimir.extensions import SessionLocal

    key = "f" * 40
    seeded = _seed_cover_series(tmp_path, key, ["v1", "v2"])
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        v2 = s.get(Article, seeded["c1@x"][0])
        src = s.execute(
            select(ArticleList).where(ArticleList.article_id == v2.id)
        ).scalar_one()
        s.add(
            ArticleList(
                article_id=v2.id,
                inbox_id=beta.id,
                epoch=src.epoch,
                commit_sha=src.commit_sha,
            )
        )
        v2.canonical_inbox_id = beta.id
        s.commit()

    page = client.get(seeded["c1@x"][1] + "/t")
    if page.status_code in (301, 302, 308):
        page = client.get(page.headers["Location"])
    links = [
        htmllib.unescape(h)
        for h in re.findall(
            r'href="([^"]*/series/[^"]*)"', _fold(page.get_data(as_text=True))
        )
    ]
    _clear_sitemap_cache()
    xml = client.get("/sitemap-series-diffs-f.xml").get_data(as_text=True)
    paths = [
        re.sub(r"^https?://[^/]+", "", htmllib.unescape(loc))
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml)
        if f"/series/{key}/" in loc
    ]
    assert links and links[0].startswith("/beta/"), (
        links
    )  # precondition: cross-post won
    assert links == paths
    assert client.get(links[0]).status_code == 200
