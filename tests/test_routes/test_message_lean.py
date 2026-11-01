"""#990: the mail reader stays lean while metadata remains on the thread."""

import re

import pytest

from mimir.config import settings
from tests.test_routes._helpers import seed_thread_shape
from tests.test_routes.test_cross_cutting import _INLINE_SCRIPT_RE


@pytest.mark.parametrize("headers", [{}, {"HX-Request": "true"}])
def test_message_navigation_changes_with_the_swapped_message(
    client, tmp_path, monkeypatch, headers
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("one@x", "root@x"), ("two@x", "one@x")]
    )
    aid, url = seeded["two@x"]
    response = client.get(url, headers=headers)
    html = response.get_data(as_text=True)
    msg = html.split('<article id="msg"', 1)[1].split("</article>", 1)[0]
    assert f'href="{seeded["root@x"][1]}/t/2#m{aid}"' in msg
    assert f'href="{seeded["one@x"][1]}"' in msg
    assert 'rel="prev"' in msg
    assert 'class="reply-to"' in msg
    assert "show:#msg:top" in msg
    assert 'class="msg-badges"' not in msg
    assert 'class="revisions-fold"' not in msg
    if not headers:
        assert html.index('id="msg"') < html.index('class="thread-context"')


def test_standalone_patch_keeps_metadata_and_revision_access(client, tmp_path):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    seeded = seed_thread_shape(tmp_path, "alpha", [("solo@x", None), ("v2@x", None)])
    with SessionLocal() as session:
        for mid, version in [("solo@x", "v1"), ("v2@x", "v2")]:
            article = session.get(Article, seeded[mid][0])
            article.subject = f"[PATCH {version}] solo patch"
            article.patch_series_key = "a" * 40
            article.patch_series_version = version
            article.patch_series_position = 1
        session.commit()
    aid, url = seeded["solo@x"]
    html = client.get(url + "/t").get_data(as_text=True)
    assert f'href="/api/alpha/metadata/{aid}"' in html
    assert html.count('class="revisions-fold"') == 1
    metadata = client.get(f"/api/alpha/metadata/{aid}")
    assert metadata.status_code == 200
    assert metadata.headers["Cache-Control"] == "no-store"
    assert metadata.headers["X-Robots-Tag"] == "noindex"
    assert re.search(r"\bstyle=", html) is None
    assert _INLINE_SCRIPT_RE.search(html) is None


def test_metadata_fragment_rejects_an_article_outside_the_inbox(client, tmp_path):
    seeded = seed_thread_shape(tmp_path, "alpha", [("only@x", None)])
    assert client.get(f"/api/beta/metadata/{seeded['only@x'][0]}").status_code == 404


def test_a_nested_series_revision_changes_the_thread_validator(client, tmp_path):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("cover@x", None), ("old@x", None), ("nested@x", "cover@x")]
    )
    with SessionLocal() as session:
        for mid, key, pos, version in [
            ("cover@x", "a" * 40, 0, "v1"),
            ("nested@x", "b" * 40, 1, "v2"),
            ("old@x", "b" * 40, 1, "v1"),
        ]:
            article = session.get(Article, seeded[mid][0])
            article.subject = f"[PATCH {version} {pos}/1] nested series"
            article.patch_series_key = key
            article.patch_series_position = pos
            article.patch_series_version = version
        session.commit()
        from mimir.patch_state import patch_timelines

        nested = session.get(Article, seeded["nested@x"][0])
        old = session.get(Article, seeded["old@x"][0])
        cover = session.get(Article, seeded["cover@x"][0])
        assert old.date <= nested.date
        assert old.id < nested.id
        assert old.patch_series_key == nested.patch_series_key != cover.patch_series_key
        assert old.patch_series_head_id == nested.patch_series_head_id
        timeline = patch_timelines(session, "b" * 40, None, [nested.id], "alpha")
        assert any(
            entry.diff_url and "from=v1&to=v2" in entry.diff_url
            for entry in timeline[nested.id]
        )
    url = seeded["cover@x"][1] + "/t"
    before = client.get(url)
    assert "from=v1&amp;to=v2" in before.get_data(as_text=True)
    with SessionLocal() as session:
        session.get(Article, seeded["old@x"][0]).patch_series_version = "v0"
        session.commit()
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert "from=v0&amp;to=v2" in after.get_data(as_text=True)


def test_collapsed_thread_details_do_not_compute_metadata(
    client, tmp_path, monkeypatch
):
    import mimir.web.routes.api as api

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    calls = []
    real = api.message_metadata

    def load(*args):
        calls.append(args[2].id)
        return real(*args)

    monkeypatch.setattr(api, "message_metadata", load)
    aid, url = seeded["reply@x"]
    thread = client.get(seeded["root@x"][1] + "/t")
    assert thread.status_code == 200
    assert f'href="/api/alpha/metadata/{aid}"' in thread.get_data(as_text=True)
    assert calls == []
    assert client.get(f"/api/alpha/metadata/{aid}").status_code == 200
    assert calls == [aid]


def test_revision_group_validator_seeks_actual_series_and_files_queries(seeded_db):
    from mimir.web.routes._validators import revision_groups_tag
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats
    from tests.test_slot_rule_plans import FILES_SEEK, KEY_SEEK, _seed_slot

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        viewed = _seed_slot(session)
        plan = plan_of(
            session,
            lambda: revision_groups_tag(session, {(viewed.patch_series_key, None)}),
            "LEFT OUTER JOIN article_lists",
        )
    assert KEY_SEEK in plan, plan
    assert FILES_SEEK in plan, plan


def test_an_inbox_parent_is_not_an_off_list_orphan(client, tmp_path):
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleFile

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("self@x", "self@x"), ("other@x", None)]
    )
    with SessionLocal() as session:
        session.add(ArticleFile(article_id=seeded["self@x"][0], path="mm/slab.c"))
        session.commit()
    response = client.get(f"/api/alpha/metadata/{seeded['self@x'][0]}")
    assert response.status_code == 200
    assert "Possibly related" not in response.get_data(as_text=True)


def test_a_nested_cover_without_patches_keeps_its_server_rendered_diff(
    client, tmp_path
):
    """Granny's #990 review: a cover may arrive without any patch messages.
    Its revision links must not depend on the patch-count/tab gate.
    """
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from mimir.patch_state import patch_timelines

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("discussion@x", None), ("oldcover@x", None), ("cover@x", "discussion@x")],
    )
    key = "c" * 40
    with SessionLocal() as session:
        for mid, version in [("oldcover@x", "v1"), ("cover@x", "v2")]:
            article = session.get(Article, seeded[mid][0])
            article.subject = f"[PATCH {version} 0/1] orphan cover"
            article.patch_series_key = key
            article.patch_series_version = version
            article.patch_series_position = 0
        session.commit()
        timeline = patch_timelines(session, key, None, [seeded["cover@x"][0]], "alpha")
        assert any(entry.diff_url for entry in timeline[seeded["cover@x"][0]])
    response = client.get(seeded["discussion@x"][1] + "/t")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="thread-tabs"' not in html
    assert 'class="patch-index"' not in html
    assert "from=v1&amp;to=v2" in html


@pytest.mark.parametrize("headers", [{}, {"HX-Request": "true"}])
def test_unmaterialised_thread_link_uses_the_thread_pages_order(
    client, tmp_path, monkeypatch, headers
):
    import html as htmllib

    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList
    from tests.test_routes._helpers import _canonical_of

    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    # DFS lists root, first, nested, second; /t lists root, first,
    # second, nested. The selected node belongs on /t/2, not DFS page1.
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [
            ("root@x", None),
            ("first@x", "root@x"),
            ("second@x", "root@x"),
            ("nested@x", "first@x"),
        ],
    )
    with SessionLocal() as session:
        session.execute(
            update(ArticleList)
            .where(ArticleList.article_id.in_([aid for aid, _ in seeded.values()]))
            .values(thread_root_id=None)
        )
        session.commit()
    aid, url = seeded["nested@x"]
    response = client.get(url, headers=headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(r'class="whole-thread" href="([^"]+)"', html)
    assert match
    target, anchor = htmllib.unescape(match.group(1)).split("#")
    assert target == seeded["root@x"][1] + "/t/2"
    assert anchor == f"m{aid}"
    destination = client.get(target)
    assert destination.status_code == 200
    assert f'id="{anchor}"' in destination.get_data(as_text=True)
    if not headers:
        assert _canonical_of(html) == "http://localhost" + url


def test_unmaterialised_thread_append_keeps_and_versions_message_kinds(
    client, tmp_path, monkeypatch
):
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("root@x", None), ("reply@x", "root@x"), ("patch@x", "reply@x")],
    )
    aid = seeded["patch@x"][0]
    with SessionLocal() as session:
        session.execute(
            update(ArticleList)
            .where(ArticleList.article_id.in_([a for a, _ in seeded.values()]))
            .values(thread_root_id=None)
        )
        patch = session.get(Article, aid)
        patch.subject = "[PATCH v1 1/1] change"
        patch.patch_series_key = "a" * 40
        patch.patch_series_version = "v1"
        patch.patch_series_position = 1
        session.commit()
    url = seeded["root@x"][1] + "/t"
    first = client.get(url)
    assert first.status_code == 200
    assert 'class="thread-tabs"' in first.get_data(as_text=True)
    full = client.get(url + "/2")
    headers = {"HX-Request": "true"}
    partial = client.get(url + "/2", headers=headers)
    for response in [full, partial]:
        assert response.status_code == 200
        tag = re.search(
            rf'<article\b[^>]*id="m{aid}"[^>]*>', response.get_data(as_text=True)
        )
        assert tag and 'data-kind="patch"' in tag.group(0)
    assert 'class="revisions-fold"' not in partial.get_data(as_text=True)
    with SessionLocal() as session:
        session.get(Article, aid).patch_series_position = 0
        session.commit()
    changed = client.get(
        url + "/2", headers={**headers, "If-None-Match": partial.headers["ETag"]}
    )
    assert changed.status_code == 200
    tag = re.search(
        rf'<article\b[^>]*id="m{aid}"[^>]*>', changed.get_data(as_text=True)
    )
    assert tag and 'data-kind="cover"' in tag.group(0)


@pytest.mark.parametrize("materialised", [True, False])
def test_appended_nested_cover_keeps_its_timeline_and_validator(
    client, tmp_path, monkeypatch, materialised
):
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList
    from mimir.web.routes import thread as thread_route

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [
            ("root@x", None),
            ("old@x", None),
            ("reply@x", "root@x"),
            ("cover@x", "root@x"),
        ],
    )
    key = "b" * 40
    with SessionLocal() as session:
        for mid, version in [("old@x", "v1"), ("cover@x", "v2")]:
            cover = session.get(Article, seeded[mid][0])
            cover.subject = f"[PATCH {version} 0/1] nested"
            cover.patch_series_key = key
            cover.patch_series_version = version
            cover.patch_series_position = 0
        if not materialised:
            session.execute(
                update(ArticleList)
                .where(ArticleList.article_id.in_([aid for aid, _ in seeded.values()]))
                .values(thread_root_id=None)
            )
        session.commit()
    calls = []
    real = thread_route.patch_timelines

    def timelines(session, series_key, head, ids, inbox):
        calls.append((series_key, tuple(ids)))
        return real(session, series_key, head, ids, inbox)

    monkeypatch.setattr(thread_route, "patch_timelines", timelines)
    url = seeded["root@x"][1] + "/t"
    headers = {"HX-Request": "true"}
    first = client.get(url, headers=headers)
    assert first.status_code == 200
    assert calls == []  # no visible non-root cover, no timeline read
    full = client.get(url + "/2")
    assert full.status_code == 200
    calls.clear()
    partial = client.get(url + "/2", headers=headers)
    assert partial.status_code == 200
    assert calls == [(key, (seeded["cover@x"][0],))]
    for response in [full, partial]:
        html = response.get_data(as_text=True)
        assert 'class="revisions-fold"' in html
        assert "from=v1&amp;to=v2" in html
    # The same revision links and diff targets survive the append.
    full_fold = (
        full.get_data(as_text=True)
        .split('class="revisions-fold"', 1)[1]
        .split("</details>", 1)[0]
    )
    partial_fold = (
        partial.get_data(as_text=True)
        .split('class="revisions-fold"', 1)[1]
        .split("</details>", 1)[0]
    )
    assert full_fold == partial_fold
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    # Capture the bounded Article lookup on a real revalidation, before
    # timeline rendering adds other reads. Both paths must seek by id.
    with planner_stats(SessionLocal, PRODUCTION_STATS), SessionLocal() as session:
        plan = plan_of(
            session,
            lambda: client.get(
                url + "/2",
                headers={**headers, "If-None-Match": partial.headers["ETag"]},
            ),
            "WHERE articles.id IN (",
        )
    assert "SEARCH articles USING INTEGER PRIMARY KEY" in plan, plan
    with SessionLocal() as session:
        session.get(Article, seeded["old@x"][0]).patch_series_version = "v0"
        session.commit()
    after = client.get(
        url + "/2", headers={**headers, "If-None-Match": partial.headers["ETag"]}
    )
    assert after.status_code == 200
    assert "from=v0&amp;to=v2" in after.get_data(as_text=True)
