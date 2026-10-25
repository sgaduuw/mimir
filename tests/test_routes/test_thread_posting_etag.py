"""#844 Part 2: the cover's ETag moves when a patch joins its posting, and
when the MAINTAINERS-derived maintainer set changes."""

from sqlalchemy import select

from mimir.models import Article, ArticleFile, ArticleList
from tests.test_routes.test_thread_posting import KEY, _seed, _visible


def test_a_patch_joining_the_posting_moves_the_cover_etag(client, tmp_path):
    from mimir.extensions import SessionLocal

    seeded = _seed(tmp_path)
    cover_url = seeded["cover@x"][1]
    urls = (cover_url + "/t", cover_url)
    before = {u: client.get(u) for u in urls}

    with SessionLocal() as s:
        src = s.execute(
            select(ArticleList).where(ArticleList.article_id == seeded["p2@x"][0])
        ).scalar_one()
        new = Article(
            message_id="p3@x",
            subject="[PATCH v2 3/3] foo: baz",
            subject_normalized="foo: baz",
            patch_series_key=KEY,
            patch_series_version="v2",
            patch_series_position=3,
        )
        s.add(new)
        s.flush()
        s.add(
            ArticleList(
                article_id=new.id,
                inbox_id=src.inbox_id,
                epoch=src.epoch,
                commit_sha=src.commit_sha,
            )
        )
        s.add(ArticleFile(article_id=new.id, path="net/zzz/joined.c"))
        s.commit()

    for u in urls:
        etag = before[u].headers["ETag"]
        after = client.get(u, headers={"If-None-Match": etag})
        assert after.status_code == 200, f"{u}: 304 after a patch joined the posting"
        assert "zzz net" in _visible(after.get_data(as_text=True)), u


def test_a_maintainer_name_that_is_an_address_is_dropped(client):
    """A MAINTAINERS name that is itself a non-allowlisted address is no
    name (#852), so the maintainer line never prints it as text."""
    from types import SimpleNamespace

    from mimir.web.filters import _subsystem_maintainers_filter

    hit = SimpleNamespace(
        maintainers=[
            ("M", "leak@example.org", "leak@example.org"),
            ("M", "Real Name", "real@example.org"),
        ]
    )
    with client.application.test_request_context():
        names = [name for name, _ in _subsystem_maintainers_filter([hit])]
    assert names == ["Real Name"], names
