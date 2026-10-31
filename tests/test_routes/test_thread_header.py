"""The thread page's header (#989): breadcrumb, prefix tags over a stripped
title, one byline and the Series, Subsystems and Maintainers cards."""

import re
from types import SimpleNamespace

COVER = "[LSF/MM/BPF TOPIC][RFC PATCH v4 0/2] Private Memory Nodes (w/ Compressed RAM)"


def _seed_header_thread(tmp_path, *, subsystems=4):
    """A cover with two patches and one reply; the patches touch one file
    in each of `subsystems` subsystems (AAA, BBB, ...), each with one
    maintainer. Returns the root's url and the article ids."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleFile
    from tests.test_routes._helpers import _seed_subsystem, seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("c@x", None), ("p1@x", "c@x"), ("p2@x", "c@x"), ("r@x", "p1@x")],
    )
    subjects = {
        "c@x": (COVER, 0),
        "p1@x": ("[PATCH v4 1/2] foo: first", 1),
        "p2@x": ("[PATCH v4 2/2] foo: second", 2),
    }
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"][:subsystems]
    for name in names:
        _seed_subsystem(
            name,
            "Maintained",
            [f"fs/{name.lower()}/"],
            [("M", f"Maint {name}", f"{name.lower()}@kernel.org")],
        )
    with SessionLocal() as s:
        for mid, (subject, pos) in subjects.items():
            a = s.get(Article, seeded[mid][0])
            a.subject = subject
            a.patch_series_key = "h" * 40
            a.patch_series_version = "v4"
            a.patch_series_position = pos
        for i, name in enumerate(names):
            aid = seeded["p1@x" if i % 2 == 0 else "p2@x"][0]
            s.add(
                ArticleFile(
                    article_id=aid,
                    path=f"fs/{name.lower()}/f.c",
                    insertions=10,
                    deletions=2,
                )
            )
        s.commit()
    return seeded


def _page(client, seeded):
    return client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)


def _header(html):
    """The page's header, up to the tabs or the first message."""
    return html[: html.index('class="thread-tabs"')]


def test_the_title_loses_its_prefixes_to_tags(client, tmp_path):
    html = _page(client, _seed_header_thread(tmp_path))
    h1 = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    assert h1 == ["Private Memory Nodes (w/ Compressed RAM)"]
    tags = re.findall(r'<li class="thread-tag[^"]*">([^<]*)</li>', html)
    assert tags == ["LSF/MM/BPF TOPIC", "RFC", "v4", "2 patches"]
    # The version tag is the accented one.
    assert re.search(r'<li class="thread-tag thread-tag-version">v4</li>', html)
    # The head is the page's identity for search and sharing: unchanged.
    assert re.search(r"<title>\[LSF/MM/BPF TOPIC\]", html)


def test_a_subject_that_is_not_a_patch_keeps_its_title_and_has_no_tags(
    client, tmp_path
):
    from tests.test_routes._helpers import _seed_three_message_thread

    seeded = _seed_three_message_thread(tmp_path, "alpha")
    html = client.get(seeded["root"][1] + "/t").get_data(as_text=True)
    assert 'class="thread-tags"' not in html
    assert len(re.findall(r"<h1", html)) == 1


def test_the_breadcrumb_climbs_from_the_thread_to_its_inbox(client, tmp_path):
    seeded = _seed_header_thread(tmp_path)
    html = _page(client, seeded)
    nav = re.search(r'<nav aria-label="breadcrumb">(.*?)</nav>', html, re.DOTALL).group(
        1
    )
    assert re.findall(r'href="([^"]+)"', nav) == [
        "/alpha/",
        "/alpha/2024/",
        "/alpha/2024/01/",
    ]
    assert re.findall(r"<li[^>]*>(?:<a[^>]*>)?([^<]+)", nav) == [
        "alpha",
        "2024",
        "January",
        "thread",
    ]
    assert 'aria-current="page"' in nav


def test_one_byline_names_the_start_and_the_counts(client, tmp_path):
    html = _page(client, _seed_header_thread(tmp_path))
    byline = re.search(r'<p class="thread-byline">(.*?)</p>', html, re.DOTALL)
    text = re.sub(r"<[^>]+>", "", byline.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    assert text.startswith("Started by ")
    assert re.search(r"on Jan \d+, 2024", text)
    assert "4 messages from 1 author" in text
    assert re.search(r"Last reply \w{3} \d+, \d{4}", text)
    assert len(re.findall('class="thread-byline"', html)) == 1


def test_a_single_message_thread_has_a_header_without_the_counts(client, tmp_path):
    from tests.test_routes._helpers import _ingest_one_article

    _, url = _ingest_one_article(tmp_path, "alpha", "solo-head@x")
    html = client.get(url + "/t").get_data(as_text=True)
    byline = re.search(r'<p class="thread-byline">(.*?)</p>', html, re.DOTALL)
    assert byline, "no byline"
    assert "Started by" in byline.group(1)
    assert "messages from" not in byline.group(1)
    assert "Last reply" not in byline.group(1)
    assert 'class="thread-tabs"' not in html
    assert len(re.findall(r"<h1", html)) == 1


def test_the_series_card_carries_the_totals_and_a_bar(client, tmp_path):
    html = _header(_page(client, _seed_header_thread(tmp_path)))
    card = re.search(
        r'<section class="thread-card" aria-labelledby="card-series">(.*?)</section>',
        html,
        re.DOTALL,
    ).group(1)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", card))
    assert "2 patches" in text
    assert "4 files" in text
    assert "+40" in text and "&minus;8" in text
    assert 'class="bar-meter"' in card
    rects = re.findall(r"<rect[^>]*>", card)
    assert len(rects) == 2  # added and removed segments
    assert "style=" not in card


def test_the_subsystems_card_shows_three_and_folds_the_rest(client, tmp_path):
    html = _header(_page(client, _seed_header_thread(tmp_path)))
    card = re.search(
        r'<section class="thread-card" aria-labelledby="card-subsystems">(.*?)</section>',
        html,
        re.DOTALL,
    ).group(1)
    assert "Subsystems (4)" in re.sub(r"<[^>]+>", "", card)
    before, _, after = card.partition("<details")
    assert len(re.findall(r'<li class="chip">', before)) == 3
    assert "Show 1 more" in after
    assert len(re.findall(r'<li class="chip">', after)) == 1


def test_this_inboxs_own_subsystem_comes_first_and_moves_the_etag(
    client, tmp_path, monkeypatch
):
    from mimir.extensions import SessionLocal
    from mimir.models import Subsystem
    from mimir.web.routes import thread as thread_route

    seeded = _seed_header_thread(tmp_path)
    url = seeded["c@x"][1] + "/t"
    with SessionLocal() as s:
        ids = {x.name: x.id for x in s.query(Subsystem)}

    def ranking(*ids_):
        monkeypatch.setattr(
            thread_route,
            "most_active_subsystems_in_inbox",
            lambda *a, **k: [SimpleNamespace(id=i) for i in ids_],
        )

    def chips(html):
        card = re.search(
            r'aria-labelledby="card-subsystems">(.*?)</section>', html, re.DOTALL
        ).group(1)
        return [
            c.lower() for c in re.findall(r'<li class="chip"><a[^>]*>([^<]*)', card)
        ]

    def maintainers(html):
        card = re.search(
            r'aria-labelledby="card-maintainers">(.*?)</section>', html, re.DOTALL
        ).group(1)
        return re.findall(r"Maint (\w+)", card)

    ranking()  # cold cache: the plain order
    cold = client.get(url)
    assert chips(cold.get_data(as_text=True)) == ["aaa", "bbb", "ccc", "ddd"]
    ranking(ids["DDD"])
    warm = client.get(url)
    html = warm.get_data(as_text=True)
    assert chips(html)[0] == "ddd"
    assert maintainers(html)[0] == "DDD"
    assert warm.headers["ETag"] != cold.headers["ETag"]
    # The same input twice is the same validator.
    assert client.get(url).headers["ETag"] == warm.headers["ETag"]


def test_the_maintainers_card_links_through_the_shared_gate(client, tmp_path):
    seeded = _seed_header_thread(tmp_path, subsystems=5)
    html = _header(_page(client, seeded))
    card = re.search(
        r'aria-labelledby="card-maintainers">(.*?)</section>', html, re.DOTALL
    ).group(1)
    assert "Maintainers (5)" in re.sub(r"<[^>]+>", "", card)
    assert "Show 1 more" in card
    # Every maintainer once, through `maintainer_link_url`; no address is
    # ever text.
    assert "@kernel.org" not in re.sub(r"<[^>]+>", "", card)
    assert sorted(re.findall(r"Maint (\w+)", card)) == [
        "AAA",
        "BBB",
        "CCC",
        "DDD",
        "EEE",
    ]
