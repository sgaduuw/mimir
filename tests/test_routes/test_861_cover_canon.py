"""#861: on a cover slot whose lowest id has no diff but a later copy
does, the fold link, the diff page's canonical and the sitemap entry are
the same bytes, and the link is served."""

import html as htmllib
import re

from sqlalchemy import select

from mimir.models import Article, ArticleFile, ArticleList, Inbox
from tests.test_routes._helpers import _clear_sitemap_cache
from tests.test_routes.test_thread import _fold, _seed_cover_series


def test_cover_slot_winner_agrees_across_fold_canonical_and_sitemap(client, tmp_path):
    from mimir.extensions import SessionLocal

    key = "e" * 40
    seeded = _seed_cover_series(tmp_path, key, ["v1", "v2", "v2"])
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        late = s.get(Article, seeded["c2@x"][0])
        src = s.execute(
            select(ArticleList).where(ArticleList.article_id == late.id)
        ).scalar_one()
        # The later v2 copy carries a diff and is canonical in beta, so a
        # reader still picking the lowest id would name alpha.
        s.add(ArticleFile(article_id=late.id, path="drivers/x/y.c"))
        s.add(
            ArticleList(
                article_id=late.id,
                inbox_id=beta.id,
                epoch=src.epoch,
                commit_sha=src.commit_sha,
            )
        )
        late.canonical_inbox_id = beta.id
        s.commit()

    page = client.get(seeded["c0@x"][1] + "/t").get_data(as_text=True)
    links = [
        htmllib.unescape(h)
        for h in re.findall(r'href="([^"]*/series/[^"]*)"', _fold(page))
    ]
    _clear_sitemap_cache()
    xml = client.get("/sitemap-series-diffs-e.xml").get_data(as_text=True)
    paths = [
        re.sub(r"^https?://[^/]+", "", htmllib.unescape(loc))
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml)
        if f"/series/{key}/" in loc
    ]
    assert links and links[0].startswith("/beta/"), links
    assert links == paths
    r = client.get(links[0])
    assert r.status_code == 200
    canon = re.search(r'<link rel="canonical" href="([^"]+)"', r.get_data(as_text=True))
    assert re.sub(r"^https?://[^/]+", "", htmllib.unescape(canon.group(1))) == links[0]
