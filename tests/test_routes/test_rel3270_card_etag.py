"""3.27.0 release review: #838's card reads the stored position, and the
reprocess (#837/#860) clears it, but the page ETag covers the series
columns only when the row has a key. A keyless row whose position the
reprocess clears changes its body under an unchanged ETag.
"""

from sqlalchemy import delete

from mimir.extensions import SessionLocal
from mimir.models import Article, CacheEntry
from tests.test_routes._helpers import _ingest_one_article


def _get(client, url):
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()
    r = client.get(url)
    assert r.status_code == 200
    return r


def test_clearing_a_keyless_rows_position_moves_the_etag(client, tmp_path):
    # Parent not in the archive: ingest stores position 2, no key.
    art_id, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "card-etag@x",
        subject="[PATCH v2 2/3] foo: bar",
        in_reply_to="missing-parent@x",
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        assert (art.patch_series_key, art.patch_series_position) == (None, 2)

    before = _get(client, url)

    # What `backfill-patch-series --reprocess` writes for a reply_patch row.
    with SessionLocal() as s:
        s.get(Article, art_id).patch_series_position = None
        s.commit()
    after = _get(client, url)

    # Precondition: the card (#838) is gone from the body.
    assert before.get_data(as_text=True) != after.get_data(as_text=True)
    assert before.headers["ETag"] != after.headers["ETag"]
