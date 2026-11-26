"""#1260: the series diff pages are served and linked from the revisions
fold, but no sitemap advertises them. Their own behaviour is covered in
`test_series_diff.py` and the fold in `test_thread.py`."""

import pytest

from tests.test_routes._helpers import _clear_sitemap_cache


def test_the_sitemap_index_lists_no_series_diff_urlset(client):
    _clear_sitemap_cache()
    xml = client.get("/sitemap.xml").get_data(as_text=True)
    assert "sitemap-maintainers.xml" in xml  # precondition: a real index
    assert "sitemap-series-diffs" not in xml


@pytest.mark.parametrize("bucket", ["0", "a", "f"])
def test_a_retired_series_diff_urlset_is_a_404(client, bucket):
    assert client.get(f"/sitemap-series-diffs-{bucket}.xml").status_code == 404
