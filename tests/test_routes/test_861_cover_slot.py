"""#861: a cover slot where another copy of the cover carries a diff. A
cover never carries a diff of its own, so the no-diff cover the reader
is looking at must keep its revisions list and state card."""

from mimir.models import ArticleFile
from tests.test_routes.test_thread import _fold, _seed_cover_series


def test_a_no_diff_cover_keeps_its_fold_when_a_copy_has_a_diff(client, tmp_path):
    from mimir.extensions import SessionLocal

    seeded = _seed_cover_series(tmp_path, "a" * 40, ["v1", "v1", "v2"])
    with SessionLocal() as s:
        s.add(ArticleFile(article_id=seeded["c1@x"][0], path="drivers/x/y.c"))
        s.commit()

    for url in (seeded["c0@x"][1] + "/t", seeded["c0@x"][1]):
        html = client.get(url).get_data(as_text=True)
        assert 'class="revisions-fold"' in html, url
        assert "v2" in _fold(html), url
