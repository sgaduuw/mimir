"""Shared helpers for tests/test_subsystems/*.py.

Hoisted from the pre-split tests/test_subsystems.py so per-
bucket test modules can import what they need. Underscore-
prefixed filename so pytest does not collect this as a test
module.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)


def _add_subsystem(
    session,
    name,
    status,
    files,
    excludes=(),
    maintainers=(),
):
    """Insert a Subsystem + its paths + maintainers in one shot.
    Returns the inserted Subsystem (with .id assigned)."""
    sub = Subsystem(name=name, status=status)
    for f in files:
        sub.paths.append(SubsystemPath(glob=f, is_exclude=False))
    for x in excludes:
        sub.paths.append(SubsystemPath(glob=x, is_exclude=True))
    for role, mname, addr in maintainers:
        sub.maintainers.append(SubsystemMaintainer(role=role, name=mname, address=addr))
    session.add(sub)
    session.flush()
    return sub


def _add_patch_article(session, msgid, paths, inbox_name="alpha"):
    """Insert a minimal Article + linked ArticleList + ArticleFile
    rows. Returns the Article id.

    The article is dated "yesterday-ish" rather than a fixed
    2024-06-01 so it stays within any default date-window filter
    the helpers under test apply (the 180-day bound on
    `recent_patches_touching` added in 1.36.3, the 7/30-day triage
    windows, etc.). Tests that exercise time-ordering set their own
    explicit dates; the default just needs to be "recent enough not
    to fall off the back of any reasonable window."
    """
    inbox = session.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
    art = Article(
        message_id=msgid,
        subject=f"patch {msgid}",
        author="a@example",
        date=datetime.now(timezone.utc) - timedelta(days=1),
        thread_parent=None,
        subject_normalized=f"patch {msgid}",
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
    )
    session.add(art)
    session.flush()
    return art.id


def _add_recent_thread_root(
    session,
    msgid,
    paths,
    subject="recent root",
    inbox_name="alpha",
):
    """Insert a recent (today-ish) article with ArticleFile rows so
    the active-threads CTE picks it up as a seed."""
    inbox = session.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
    art = Article(
        message_id=msgid,
        subject=subject,
        author="a@example",
        date=datetime.now(timezone.utc) - timedelta(hours=1),
        thread_parent=None,
        subject_normalized=subject,
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
    )
    session.add(art)
    session.flush()
    return art


def _add_recent_patch_with_trailers(
    session,
    msgid,
    paths,
    trailers,
    inbox_name="alpha",
    days_ago=0,
):
    """Insert a recent article with ArticleFile rows + ArticleTrailer
    rows. `trailers` is a list of (role, name, address) tuples."""
    inbox = session.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
    art = Article(
        message_id=msgid,
        subject=f"patch {msgid}",
        author="a@example",
        date=datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1),
        thread_parent=None,
        subject_normalized=f"patch {msgid}",
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
        trailers=[
            ArticleTrailer(
                role=role,
                name=name,
                address=addr,
                address_normalized=addr.lower(),
            )
            for role, name, addr in trailers
        ],
    )
    session.add(art)
    session.flush()
    return art
