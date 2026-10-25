"""Tests for mimir/cli/devseed.py: `dev-seed-thread` (creates
inbox + articles, real thread structure, idempotent on re-
run, message-URL reachable through the route, inbox-name
validator)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli.devseed import dev_seed_thread_command
from mimir.models import Article, ArticleList, Inbox


def test_dev_seed_thread_creates_inbox_and_articles(seeded_db, tmp_path):
    """First invocation: creates the inbox, ingests N messages, prints a
    URL pointing at a real article. Hermetic via --mirror-root."""
    from mimir.extensions import SessionLocal

    mirror_root = tmp_path / "Inboxes"
    result = CliRunner().invoke(
        dev_seed_thread_command,
        [
            "--inbox",
            "dev-thread-test",
            "--messages",
            "5",
            "--mirror-root",
            str(mirror_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "created inbox 'dev-thread-test'" in result.output
    assert "new=5" in result.output  # all 5 are fresh
    assert "navigate to: http://" in result.output

    # Mirror layout on disk matches the documented shape.
    assert (mirror_root / "dev-thread-test" / "git" / "0.git").is_dir()

    # DB rows match.
    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-test")
        ).scalar_one()
        assert ix.mirror_path == str(mirror_root / "dev-thread-test" / "git")
        rows = s.execute(
            select(Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == ix.id)
        ).all()
        assert len(rows) == 5


def test_dev_seed_thread_forms_a_real_thread(seeded_db, tmp_path):
    """Every reply must reference an in-archive parent so the
    recursive-CTE walk-up (`find_thread_root`) terminates and renders
    a tree. Without this guarantee, the dev-seeded inbox would render
    every message as its own root, defeating the whole point of the
    helper (which is to give the thread-fold UI something to display)."""
    from mimir.extensions import SessionLocal

    result = CliRunner().invoke(
        dev_seed_thread_command,
        [
            "--inbox",
            "dev-thread-shape",
            "--messages",
            "6",
            "--mirror-root",
            str(tmp_path / "Inboxes"),
        ],
    )
    assert result.exit_code == 0, result.output

    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-shape")
        ).scalar_one()
        articles = list(
            s.execute(
                select(Article)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(ArticleList.inbox_id == ix.id)
                .order_by(Article.date.asc())
            ).scalars()
        )

    assert len(articles) == 6
    # First article is the root: no thread_parent.
    assert articles[0].thread_parent is None, (
        "first seeded message must be the thread root"
    )
    # All replies reference a parent message-id that exists in the same
    # set of seeded articles -- no off-list ancestors.
    seeded_ids = {a.message_id for a in articles}
    for a in articles[1:]:
        assert a.thread_parent is not None
        assert a.thread_parent in seeded_ids, (
            f"reply {a.message_id} references off-list parent "
            f"{a.thread_parent}; dev-seed should keep the thread closed"
        )


def test_dev_seed_thread_idempotent_appends_on_rerun(seeded_db, tmp_path):
    """Re-running against an existing inbox doesn't recreate or
    error out; it appends fresh messages to the same repo. The
    CLI's `using existing inbox` log line is the operator-visible
    signal that the second run took the append path."""
    from mimir.extensions import SessionLocal

    args = [
        "--inbox",
        "dev-thread-idempotent",
        "--messages",
        "3",
        "--mirror-root",
        str(tmp_path / "Inboxes"),
    ]
    first = CliRunner().invoke(dev_seed_thread_command, args)
    assert first.exit_code == 0, first.output
    assert "created inbox 'dev-thread-idempotent'" in first.output

    second = CliRunner().invoke(dev_seed_thread_command, args)
    assert second.exit_code == 0, second.output
    assert "using existing inbox 'dev-thread-idempotent'" in second.output

    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-idempotent")
        ).scalar_one()
        rows = s.execute(
            select(Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == ix.id)
        ).all()
    # Each run adds 3; two runs => 6 total.
    assert len(rows) == 6


def test_dev_seed_thread_repoints_existing_inbox_to_new_mirror(seeded_db, tmp_path):
    """A re-run with another --mirror-root repoints the inbox, so the
    broker ingests the mirror this run wrote rather than the old one."""
    from mimir.extensions import SessionLocal

    def run(root):
        args = ["--inbox", "dev-thread-moved", "--messages", "3"]
        return CliRunner().invoke(
            dev_seed_thread_command, [*args, "--mirror-root", str(root)]
        )

    assert run(tmp_path / "a").exit_code == 0
    second = run(tmp_path / "b")
    assert second.exit_code == 0, second.output
    assert re.search(r"\bnew=(\d+)", second.output).group(1) == "3"
    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-moved")
        ).scalar_one()
    assert ix.mirror_path == str(tmp_path / "b" / "dev-thread-moved" / "git")


def test_dev_seed_thread_message_url_is_reachable(client, seeded_db, tmp_path):
    """The printed URL must resolve to a 200 against the running app.
    Extracts the URL from CLI output and hits it; if dev-seed has
    silently drifted (wrong path format, missing blob, etc.) this
    catches it."""
    import re

    result = CliRunner().invoke(
        dev_seed_thread_command,
        [
            "--inbox",
            "dev-thread-routable",
            "--messages",
            "4",
            "--mirror-root",
            str(tmp_path / "Inboxes"),
        ],
    )
    assert result.exit_code == 0, result.output
    m = re.search(r"navigate to: http://[^/]+(/\S+)", result.output)
    assert m is not None, f"no URL in output: {result.output}"
    url_path = m.group(1)
    r = client.get(url_path)
    assert r.status_code == 200, (
        f"dev-seed URL {url_path} returned {r.status_code}; "
        f"the seed helper has likely drifted from the route shape"
    )


@pytest.mark.parametrize("broker_available", [False, True])
def test_dev_seed_thread_from_separate_read_only_process(
    client, seeded_db, tmp_path, broker_available
):
    """A real dev-seed run is a separate process with `query_only=1`
    connections and no writer context, unlike this suite, which runs
    in-process as the writer. Every write must go over the broker socket,
    and the seeded thread must render with its fold toolbar (#932). The
    run starts in another directory with a relative --mirror-root, so the
    broker finds the mirror only if the stored path is absolute."""
    from mimir.config import settings

    assert Path.cwd() != tmp_path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mimir.cli.devseed import dev_seed_thread_command; "
            "dev_seed_thread_command()",
            "--inbox",
            "dev-thread-rpc",
            "--messages",
            "5",
            "--mirror-root",
            "Inboxes",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "MIMIR_IS_BROKER": "false",
            "BROKER_SOCKET_PATH": str(
                settings.broker_socket_path
                if broker_available
                else tmp_path / "missing.sock"
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    if not broker_available:
        assert result.returncode != 0
        # A ClickException naming the broker, not a write traceback.
        assert "Traceback" not in result.stderr, result.stderr
        assert "broker" in result.stderr.lower(), result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    m = re.search(r"navigate to: http://[^/]+(/\S+)", result.stdout)
    assert m is not None, result.stdout
    r = client.get(m.group(1))
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    total = re.search(r"Thread \((\d+) messages\)", body)
    assert total is not None, "message page rendered no thread toolbar"
    assert int(total.group(1)) == 5
    assert 'data-fold-set="closed"' in body


# Shared helpers for the blob-touching commands (ingest / reindex / show).
# A local copy beats importing private helpers across test files; if the
# public-inbox v2 layout ever changes, both files update together but
# neither has to depend on the other.


def test_dev_seed_thread_rejects_invalid_inbox_name(seeded_db, tmp_path):
    """The dev-seed-thread inbox_name flows into both the
    filesystem path (`<mirror_root>/<inbox_name>/git`) and the
    RFC 5322 To: header bytes for each synthesised message.
    Validation via the same slug regex the admin service uses
    catches `..`, slashes, CR/LF, uppercase, and shell metachars
    in one shot."""
    for bad in (
        "../escape",  # path traversal
        "Inbox",  # uppercase (slug must be lowercase)
        "inbox/sub",  # slash
        "inbox\r\nTo:",  # CRLF header-injection vector
        "-leading",  # validator forbids hyphen at edges
        "",  # empty
    ):
        result = CliRunner().invoke(
            dev_seed_thread_command,
            ["--inbox", bad, "--mirror-root", str(tmp_path)],
        )
        assert result.exit_code != 0, (
            f"inbox_name {bad!r} should be rejected, "
            f"got exit_code=0\noutput: {result.output}"
        )
        # No mirror dir for the rejected name should have been
        # created -- the validation has to happen before any side
        # effect.
        if bad and "/" not in bad and "\r" not in bad and "\n" not in bad:
            assert not (tmp_path / bad).exists()


# `show` -- one-article inspect, with the three ClickException branches.
