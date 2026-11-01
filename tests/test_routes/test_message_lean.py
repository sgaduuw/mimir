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
