"""Closed Linux releases: immutable Git ranges and expiring recorded links."""

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.objects import Commit, Tag
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.datetime_utils import aware_utc
from mimir.extensions import SessionLocal
from mimir.merge_window import _VERSION_TAG_RE, MERGE_WINDOW_PATH, _linus_path
from mimir.models import Article, ArticleList

logger = logging.getLogger(__name__)
MERGE_WINDOW_HISTORY_PATH = MERGE_WINDOW_PATH + "/history"
GIT_TTL_SEC = 30 * 86400
LINKS_TTL_SEC = 86400
GIT_TIMEOUT_SEC = 30
PAGE_SIZE = 50


def is_release_tag(tag: str) -> bool:
    match = _VERSION_TAG_RE.fullmatch(tag)
    return match is not None and match.group(2) is None


def release_history_path(tag: str | None = None, *, page: int = 1) -> str:
    if tag is None:
        return MERGE_WINDOW_HISTORY_PATH
    if not is_release_tag(tag):
        raise ValueError("not a final release tag")
    if page < 1:
        raise ValueError("page must be positive")
    suffix = f"?page={page}" if page > 1 else ""
    return f"{MERGE_WINDOW_HISTORY_PATH}/{tag}{suffix}"


@dataclass(frozen=True)
class Release:
    tag: str
    path: Path
    oid: str
    previous_tag: str | None
    previous_oid: str | None
    rc_refs: tuple[tuple[str, str], ...]
    key: str


class ReleaseFacts(BaseModel):
    release_date: datetime | None
    date_source: str | None
    merge_days: int | None
    stabilization_days: int | None
    rc_count: int
    commit_count: int | None


class ReleaseLinks(BaseModel):
    pair_count: int | None
    computed_at: datetime


def _link_payload(
    release: Release, pair_count: int | None, message_ids: list[str]
) -> dict:
    summary = ReleaseLinks(pair_count=pair_count, computed_at=datetime.now(UTC))
    cache.set(release.key + ":links", summary.model_dump(mode="json"), LINKS_TTL_SEC)
    # Detail reads the summary and IDs atomically, even when two refreshes
    # race. The small index copy need not decode every historical message ID.
    return {"summary": summary.model_dump(mode="json"), "message_ids": message_ids}


def cached_links(releases: list[Release]) -> dict[str, ReleaseLinks]:
    values = cache.get_many([r.key + ":links" for r in releases])
    return {
        r.tag: ReleaseLinks.model_validate(values[r.key + ":links"])
        for r in releases
        if r.key + ":links" in values
    }


def release_catalog(path: Path | None = None) -> list[Release]:
    """Read refs only; opening the index never starts a history walk."""
    path = path if path is not None else _linus_path()
    if path is None:
        return []
    try:
        with Repo(str(path)) as repo:
            refs = {
                name.decode("ascii"): oid.decode("ascii")
                for name, oid in repo.refs.as_dict(b"refs/tags").items()
                if name.isascii() and _VERSION_TAG_RE.fullmatch(name.decode("ascii"))
            }
        stat = path.stat()
    except NotGitRepository, OSError:
        return []
    finals = sorted(
        (name for name in refs if is_release_tag(name)),
        key=lambda name: tuple(map(int, name[1:].split("."))),
    )
    releases = []
    previous = None
    for tag in finals:
        rc_refs = tuple(
            sorted((n, v) for n, v in refs.items() if n.startswith(tag + "-rc"))
        )
        previous_oid = refs[previous] if previous else None
        identity = (
            str(path.resolve()),
            stat.st_dev,
            stat.st_ino,
            tag,
            refs[tag],
            previous_oid,
            rc_refs,
        )
        key = "release_history:1:" + hashlib.sha256(repr(identity).encode()).hexdigest()
        releases.append(
            Release(tag, path, refs[tag], previous, previous_oid, rc_refs, key)
        )
        previous = tag
    return releases[::-1]


def _tag_metadata(repo: Repo, oid: str | None):
    if oid is None:
        return None, None, None
    obj = repo[oid.encode("ascii")]
    tagged_at = obj.tag_time if isinstance(obj, Tag) else None
    while isinstance(obj, Tag):
        obj = repo[obj.object[1]]
    commit = obj.id.decode("ascii") if isinstance(obj, Commit) else None
    if tagged_at is not None:
        return commit, datetime.fromtimestamp(tagged_at, UTC), "tagger"
    if isinstance(obj, Commit):
        return commit, datetime.fromtimestamp(obj.commit_time, UTC), "commit"
    return None, None, None


def _days(start: datetime | None, end: datetime | None) -> int | None:
    # A retagged or incomplete history must not invent a negative phase.
    if start is None or end is None or end < start:
        return None
    return (end.date() - start.date()).days


def calculate_git(release: Release) -> tuple[ReleaseFacts, list[str] | None]:
    """One bounded rev-list supplies both membership and its count.

    Use object IDs captured with the cache key, not refs that a concurrent
    fetch may move. Annotated tag dates are tagger dates; lightweight tags
    use commit dates. The current merge-window anchor remains unchanged.
    """
    with Repo(str(release.path)) as repo:
        current, date, source = _tag_metadata(repo, release.oid)
        previous, previous_date, _ = _tag_metadata(repo, release.previous_oid)
        rc1_oid = dict(release.rc_refs).get(release.tag + "-rc1")
        _, rc1_date, _ = _tag_metadata(repo, rc1_oid)
    members = None
    if previous and current:
        output = subprocess.run(
            ["git", "-C", str(release.path), "rev-list", f"{previous}..{current}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SEC,
        ).stdout
        members = output.splitlines()
    return ReleaseFacts(
        release_date=date,
        date_source=source,
        merge_days=_days(previous_date, rc1_date),
        stabilization_days=_days(rc1_date, date),
        rc_count=len(release.rc_refs),
        commit_count=len(members) if members is not None else None,
    ), members


def git_facts(release: Release) -> tuple[ReleaseFacts, list[str] | None]:
    """Keep membership separate so an index cache hit decodes only facts."""
    values = cache.get_many([release.key + ":facts", release.key + ":members"])
    facts, members = (
        values.get(release.key + ":facts"),
        values.get(release.key + ":members"),
    )
    if facts is not None and release.key + ":members" in values:
        return ReleaseFacts.model_validate(facts), members
    facts, members = calculate_git(release)
    # Feature-versioned plain payloads leave every pre-existing cache type
    # and namespace valid during a rolling deployment or rollback.
    cache.set(release.key + ":members", members, GIT_TTL_SEC)
    cache.set(release.key + ":facts", facts.model_dump(mode="json"), GIT_TTL_SEC)
    return facts, members


def cached_facts(releases: list[Release]) -> dict[str, ReleaseFacts]:
    values = cache.get_many([r.key + ":facts" for r in releases])
    return {
        r.tag: ReleaseFacts.model_validate(values[r.key + ":facts"])
        for r in releases
        if r.key + ":facts" in values
    }


def calculate_links(
    session: Session, members: list[str] | None
) -> tuple[int | None, list[str]]:
    if members is None:
        return None, []
    count = 0
    message_ids = set()
    # Production measurements found the tree index rescanned all Linus rows
    # per batch without statistics. Pin the SHA-leading covering primary key;
    # SQLAlchemy's SQLite with_hint does not emit INDEXED BY.
    statement = text(
        "SELECT message_id FROM mainline_commits "
        "INDEXED BY sqlite_autoindex_mainline_commits_1 "
        "WHERE commit_sha IN :shas AND tree_name = 'linus'"
    ).bindparams(bindparam("shas", expanding=True))
    for chunk in batched(members, 900):
        rows = session.scalars(statement, {"shas": chunk}).all()
        count += len(rows)
        message_ids.update(rows)
    return count, sorted(message_ids)


def release_data(release: Release) -> tuple[ReleaseFacts, ReleaseLinks, list[str]]:
    """Compute at most one range, without holding a DB snapshot during Git."""
    facts = None
    with SessionLocal() as session:

        def compute():
            nonlocal facts
            # This session belongs to this helper, not its caller. End the
            # cache lookup's transaction before walking Git or writing caches.
            session.rollback()
            facts, members = git_facts(release)
            pair_count, message_ids = calculate_links(session, members)
            session.rollback()
            return _link_payload(release, pair_count, message_ids)

        payload = cache.get_or_compute(
            session, release.key + ":links_data", LINKS_TTL_SEC, compute
        )
    # Broker writes may still be queued, or a best-effort write may have
    # failed. Never redo the Git walk to read back facts already calculated.
    summary = ReleaseLinks.model_validate(payload["summary"])
    if facts is None:
        small = cache.get_many([release.key + ":facts", release.key + ":links"])
        raw_facts = small.get(release.key + ":facts")
        facts = ReleaseFacts.model_validate(raw_facts) if raw_facts else None
        if release.key + ":links" not in small:
            # A dropped/interrupted small write must not leave the index
            # pending all day. Repair only the original remaining lifetime.
            remaining = int(
                summary.computed_at.timestamp() + LINKS_TTL_SEC - cache._now()
            )
            if remaining > 0:
                cache.set(release.key + ":links", payload["summary"], remaining)
    if facts is None:
        facts, _ = git_facts(release)
    return (
        facts,
        summary,
        payload["message_ids"],
    )


def warm_release(session: Session, release: Release) -> None:
    """Adapter for the slow warmer's shared, read-only target session."""
    # Earlier global targets can leave their read transaction open. This
    # adapter alone ends it; general service helpers never roll back callers.
    session.rollback()
    try:
        release_data(release)
    finally:
        session.rollback()


def archived_page(
    session: Session, message_ids: list[str], page: int
) -> tuple[list[tuple[Article, str]], int]:
    """Intersect cached IDs with today's archive; load metadata for one page.

    Lightweight ID/date batches bound SQL variables without caching authors,
    inbox ownership or thread roots. A newly archived message therefore appears
    even while the release's recorded-reference cache remains valid.
    """
    from mimir.web.urls import _advertised_urls_for

    candidates = []
    for chunk in batched(set(message_ids), 900):
        candidates.extend(
            session.execute(
                select(Article.id, Article.date).where(
                    Article.message_id.in_(chunk),
                    Article.date.is_not(None),
                    select(ArticleList.article_id)
                    .where(ArticleList.article_id == Article.id)
                    .exists(),
                )
            ).all()
        )
    candidates.sort(
        key=lambda row: (
            aware_utc(row.date),
            row.id,
        ),
        reverse=True,
    )
    selected = [row.id for row in candidates[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]]
    if not selected:
        return [], len(candidates)
    articles = list(session.scalars(select(Article).where(Article.id.in_(selected))))
    by_id = {article.id: article for article in articles}
    urls = _advertised_urls_for(session, articles)
    return [(by_id[aid], urls[aid]) for aid in selected if aid in urls], len(candidates)
