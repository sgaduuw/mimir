"""Shared helpers for tests/test_routes/*.py.

Hoisted from the pre-split tests/test_routes.py so per-bucket
test modules can import what they need. Underscore-prefixed
filename so pytest does not collect this as a test module.

Each helper keeps its imports inside the function body (the
same shape as the pre-split file): the helpers exercise different
subsystems and import-at-call avoids dragging every test-time
dependency into the module's load path.
"""


def _clear_sitemap_cache():
    """Sitemap routes cache their XML in the `cache` table; cross-test
    mutations (e.g. flipping `art3.canonical_inbox_id`) won't be
    visible until the cached rows expire. Tests that need a fresh
    render call this first."""
    from sqlalchemy import delete
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()


# Endpoints that don't depend on a configured inbox.


def _parse_csp(csp: str) -> dict[str, list[str]]:
    """Parse a CSP header into a {directive: [sources, ...]} map.
    Robust to whitespace, directive order, and the `script-src` vs
    `script-src-elem` substring trap."""
    out: dict[str, list[str]] = {}
    for part in csp.split(";"):
        tokens = part.split()
        if not tokens:
            continue
        out[tokens[0]] = tokens[1:]
    return out


def _any_article_in(inbox_name):
    """Helper: return one (Article, inbox_name) pair from the running
    DB so the redirect tests have a real Message-ID to point at."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        return s.execute(
            select(Article)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(Inbox.name == inbox_name)
            .limit(1)
        ).scalar_one_or_none()


def _build_app_with_hops(monkeypatch, hops: int):
    """Re-create the Flask app with `trusted_proxy_hops` patched.
    `create_app()` decides ProxyFix wiring at construction time."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", hops)
    from mimir import create_app

    return create_app()


def _title_of(html: str) -> str:
    import re

    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    return m.group(1).strip() if m else ""


def _ingest_one_article(
    tmp_path,
    inbox_name: str,
    message_id: str,
    subject: str = "test",
    in_reply_to: str | None = None,
    to: str | None = None,
    author: str = "a@b.example",
    body: bytes = b"body",
) -> tuple[int, str]:
    """Build a tiny pubinbox-shaped bare repo with one message and
    ingest it into `inbox_name`. Repoints the inbox's mirror_path at
    `tmp_path` so `read_message` can fetch the blob; returns
    `(article_id, /<inbox>/YYYY/MM/<id>)` for routing tests."""
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    extra = b""
    if in_reply_to is not None:
        extra += b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n"
    if to is not None:
        extra += b"To: " + to.encode() + b"\r\n"
    raw = (
        b"Message-ID: <" + message_id.encode() + b">\r\n"
        b"From: " + author.encode() + b"\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n" + extra + b"\r\n" + body
    )
    repo_dir = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    blob = Blob.from_string(raw)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"m", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = []
    commit.author = commit.committer = b"test <t@x>"
    # Recent commit timestamp (yesterday-ish) so the article stays
    # within any default time-window filter applied downstream
    # (`recent_patches_max_age_days`=180 from 1.36.3, active-thread
    # windows, etc.). `Article.date` comes from this commit time
    # rather than the RFC 5322 `Date:` header per the public-inbox-
    # as-source-of-truth design (CONTEXT.md). A fixed 2023 timestamp
    # would now fall off the back of every recency window. Tests
    # that exercise URL date prefixes infer the year/month from
    # the returned URL rather than hardcoding 2023/11.
    import time as _time

    commit.commit_time = commit.author_time = int(_time.time()) - 86400
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"add"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", repo_dir, workers=1)
        art = s.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one()
        url = f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}"
        return art.id, url


def _meta_value(html: str, name_or_property: str) -> str | None:
    """Extract a <meta> tag's content. Matches both `name=` and
    `property=` since OG uses property and the rest use name."""
    import re

    pattern = (
        r'<meta\s+(?:property|name)="'
        + re.escape(name_or_property)
        + r'"\s+content="([^"]*)"'
    )
    m = re.search(pattern, html)
    return m.group(1) if m else None


def _data_attr_values(html: str) -> str:
    """Concatenate every `data-…="…"` attribute value in the document
    into a single string for `not-in` checks. Used to defend the
    invariant that machine-readable attributes never carry tokens
    (Message-IDs, raw email addresses) that the visible HTML
    redacted."""
    import re

    return "\n".join(re.findall(r'data-[a-z0-9-]+="([^"]*)"', html))


def _json_ld_blocks(html: str) -> list[dict]:
    """Extract every <script type=application/ld+json> JSON payload."""
    import json
    import re

    out: list[dict] = []
    for m in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        re.DOTALL,
    ):
        out.append(json.loads(m.group(1)))
    return out


def _seed_three_message_thread(tmp_path, inbox_name):
    """Build a 3-message thread root → reply → reply-to-reply in ONE
    shared bare repo, so the inbox's mirror_path resolves blobs for
    every article (not just the last-ingested one).

    `_ingest_one_article` creates its own fresh `0.git` per call and
    repoints mirror_path; that's fine for single-message tests but
    breaks here -- viewing the root would 500 trying to look up the
    root's commit_sha in the *reply's* mirror. This helper appends
    each message as a separate commit in the same bare repo so all
    three resolve under one mirror_path.

    Returns a dict keyed by role ("root" / "reply" / "nested") with
    `(article_id, url, message_id)` tuples for each.
    """
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    repo_dir = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    prev_commit = None

    def _add(message_id: str, subject: str, in_reply_to: str | None) -> None:
        nonlocal prev_commit
        extra = b""
        if in_reply_to is not None:
            extra += b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n"
        raw = (
            b"Message-ID: <" + message_id.encode() + b">\r\n"
            b"From: a@b.example\r\n"
            b"Subject: " + subject.encode() + b"\r\n"
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n" + extra + b"\r\n"
            b"body"
        )
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [prev_commit] if prev_commit else []
        commit.author = commit.committer = b"test <t@x>"
        # 1704067200 == 2024-01-01 00:00 UTC; bumped per message so each
        # commit is unique and threading order is stable.
        commit.commit_time = commit.author_time = 1704067200 + len(_added)
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = b"add"
        repo.object_store.add_object(commit)
        prev_commit = commit.id
        _added.append(message_id)

    _added: list[str] = []
    _add("fold-root@example.com", "root msg", None)
    _add("fold-reply@example.com", "Re: root msg", "fold-root@example.com")
    _add("fold-nested@example.com", "Re: root msg", "fold-reply@example.com")
    repo.refs[b"HEAD"] = prev_commit

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", repo_dir, workers=1)
        out = {}
        for role, mid in [
            ("root", "fold-root@example.com"),
            ("reply", "fold-reply@example.com"),
            ("nested", "fold-nested@example.com"),
        ]:
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            url = f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}"
            out[role] = (art.id, url, mid)
        return out


def _seed_subsystem(name, status, files, maintainers=()):
    """Slot a Subsystem row in for route-side render tests. Keeps
    each test self-contained; the autouse `_reset_db` wipes
    between tests."""
    from mimir.extensions import SessionLocal
    from mimir.models import Subsystem, SubsystemMaintainer, SubsystemPath

    with SessionLocal() as s:
        sub = Subsystem(name=name, status=status)
        for f in files:
            sub.paths.append(SubsystemPath(glob=f, is_exclude=False))
        for role, mname, addr in maintainers:
            sub.maintainers.append(
                SubsystemMaintainer(role=role, name=mname, address=addr)
            )
        s.add(sub)
        s.commit()


def _seed_mainline_commit(
    message_id, commit_sha="abc1234567890def" + "0" * 24, tree_name="linus", date=None
):
    """Insert a MainlineCommit row for a route test. The render
    side reads commit_sha (truncated to 12 chars), tree_name, and
    committed_at."""
    from datetime import datetime, timezone
    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha=commit_sha,
                message_id=message_id,
                tree_name=tree_name,
                committed_at=date or datetime(2024, 6, 1, tzinfo=timezone.utc),
            )
        )
        s.commit()


def _ingest_with_attachment(
    tmp_path,
    inbox_name: str,
    message_id: str,
    *,
    attachment_filename: str,
    attachment_content_type: str,
    attachment_bytes: bytes,
) -> str:
    """Build + ingest a multipart/mixed message with a single
    base64-encoded attachment. Returns the article URL prefix
    (without `/attachment/...`)."""
    import base64
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    b64 = base64.b64encode(attachment_bytes).decode()
    # Chunk to RFC-compatible 76-char lines.
    b64_chunked = "\r\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))

    raw = (
        b"Message-ID: <" + message_id.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: with attachment\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b'Content-Type: multipart/mixed; boundary="bnd"\r\n'
        b"\r\n"
        b"--bnd\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"the body\r\n"
        b"--bnd\r\n"
        b"Content-Type: " + attachment_content_type.encode() + b"\r\n"
        b'Content-Disposition: attachment; filename="'
        + attachment_filename.encode()
        + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + b64_chunked.encode() + b"\r\n"
        b"--bnd--\r\n"
    )

    repo_dir = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    blob = Blob.from_string(raw)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"m", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    c = Commit()
    c.tree = tree.id
    c.parents = []
    c.author = c.committer = b"t <t@x>"
    c.commit_time = c.author_time = 1704067200
    c.commit_timezone = c.author_timezone = 0
    c.encoding = b"UTF-8"
    c.message = b"add"
    repo.object_store.add_object(c)
    repo.refs[b"HEAD"] = c.id

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", repo_dir, workers=1)
        art = s.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one()
        return f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}"


def _seed_author_article(inbox_name: str, *, author: str, message_id: str) -> int:
    """Insert one Article row tied to the given inbox with a chosen
    author string. Returns the Article id."""
    from datetime import datetime, timezone

    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        art = Article(
            message_id=message_id,
            subject="author route subject",
            author=author,
            date=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="author route subject",
        )
        s.add(art)
        s.flush()
        s.add(
            ArticleList(
                article_id=art.id,
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha="aa" * 20,
            )
        )
        s.commit()
        return art.id


def _build_pubinbox_epoch(epoch_dir, messages):
    """Build a chained-commit bare git repo simulating a public-inbox
    epoch with multiple messages. Each tuple is
    `(message_id, subject, in_reply_to, body, author)`; in_reply_to and
    body may be None / b"". Returns nothing; the caller arranges
    `Inbox.mirror_path` to point at the parent dir before ingesting.
    """
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    repo = Repo.init_bare(str(epoch_dir), mkdir=True)
    parent = None
    last_commit_id = None
    for msgid, subject, in_reply_to, body, author in messages:
        extra = b""
        if in_reply_to:
            extra += b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n"
        raw = (
            b"Message-ID: <" + msgid.encode() + b">\r\n"
            b"From: " + author.encode() + b"\r\n"
            b"Subject: " + subject.encode() + b"\r\n"
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
            + extra
            + b"\r\n"
            + (body or b"")
        )
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1700000000
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = f"add {msgid}".encode()
        repo.object_store.add_object(commit)
        parent = commit.id
        last_commit_id = commit.id
    repo.refs[b"HEAD"] = last_commit_id


def _ingest_series_pair(tmp_path, inbox_name, v1_messages, v2_messages):
    """Build v1 and v2 epochs in `tmp_path/0.git` and `tmp_path/1.git`,
    repoint `inbox_name.mirror_path` to `tmp_path`, ingest both.
    Returns the cover letter's `patch_series_key` (the same for both
    revisions by construction)."""
    from sqlalchemy import select as _sa_select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    _build_pubinbox_epoch(tmp_path / "0.git", v1_messages)
    _build_pubinbox_epoch(tmp_path / "1.git", v2_messages)
    with SessionLocal() as s:
        ix = s.execute(_sa_select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", tmp_path / "0.git", workers=1)
        ingest_epoch(s, ix, "1.git", tmp_path / "1.git", workers=1)
        s.commit()
        v1_cover_msgid = v1_messages[0][0]
        cover = s.execute(
            _sa_select(Article).where(Article.message_id == v1_cover_msgid)
        ).scalar_one()
        assert cover.patch_series_key is not None
        return cover.patch_series_key


def seed_thread_shape(tmp_path, inbox_name, edges, *, date_for=None, epoch="0.git"):
    """Build an ARBITRARY thread shape in one bare repo and ingest it.

    `edges` is an ordered list of `(message_id, parent_message_id|None)`.
    Cycles, self-parents, branches, and forward references are all
    expressible, because the point of this helper is to exercise the
    shapes real ingest can produce from attacker- or
    accident-controlled `In-Reply-To` headers, not just the tidy linear
    thread `_seed_three_message_thread` builds.

    `date_for` optionally maps message_id -> RFC 5322 Date string, for
    month/year-boundary shapes. `epoch` places the messages in a
    specific epoch repo, for shapes that span epochs (what
    `reindex --from-scratch` operates on).

    Returns `{message_id: (article_id, url)}`.
    """
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    repo_dir = tmp_path / epoch
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    prev = None
    for i, (mid, parent) in enumerate(edges):
        extra = b""
        if parent is not None:
            extra += b"In-Reply-To: <" + parent.encode() + b">\r\n"
        date = (date_for or {}).get(mid, "Mon, 1 Jan 2024 00:00:00 +0000")
        raw = (
            b"Message-ID: <" + mid.encode() + b">\r\n"
            b"From: a@b.example\r\n"
            b"Subject: shape msg\r\n"
            b"Date: " + date.encode() + b"\r\n" + extra + b"\r\n"
            b"body"
        )
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [prev] if prev else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1704067200 + i
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = b"add"
        repo.object_store.add_object(commit)
        prev = commit.id
    repo.refs[b"HEAD"] = prev

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, epoch, repo_dir, workers=1)
        out = {}
        for mid, _parent in edges:
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            out[mid] = (
                art.id,
                f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}",
            )
        return out
