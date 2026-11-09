"""Closed Linux releases: immutable Git ranges and expiring recorded links."""

import fcntl
import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
from mimir.merge_window import (
    _VERSION_TAG_RE,
    MERGE_WINDOW_PATH,
    MergeWindowRow,
    MergeWindowSection,
    _linus_path,
    primary_subsystem,
    rank_postings,
    subsystem_sections,
)
from mimir.models import Article, ArticleList
from mimir.patch_series import series_head_sql, version_number_sql

logger = logging.getLogger(__name__)
MERGE_WINDOW_HISTORY_PATH = MERGE_WINDOW_PATH + "/history"
GIT_TTL_SEC = 30 * 86400
LINKS_TTL_SEC = 86400
GIT_TIMEOUT_SEC = 30
PAGE_SIZE = 50
# Below this many archived references a subsystem table is near-empty, so
# the page says so in one line instead. A judgement, not a measurement.
HANDFUL_OF_REFERENCES = 10


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


class ReleaseSubsystems(BaseModel):
    """A release's archived references grouped as `/merge-window` groups
    its queue. The sections carry counts, no rows: the references are
    listed on the page, paginated."""

    sections: list[MergeWindowSection]
    smaller: list[MergeWindowSection]
    smaller_patches: int
    # Distinct archived references counted, what every bar is a share of.
    references: int


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


def calculate_git(
    release: Release, timeout: float = GIT_TIMEOUT_SEC
) -> tuple[ReleaseFacts, list[str] | None]:
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
            timeout=timeout,
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


# One lock per release key per process, never removed: keys change only
# when a tag moves, so the dict holds about one entry per release.
_walk_locks: dict[str, threading.Lock] = {}
_walk_locks_guard = threading.Lock()
_LOCK_POLL_SEC = 0.05


@contextmanager
def _single_flight(key: str, deadline: float) -> Iterator[None]:
    """Exclude other walks of `key`, across threads and gunicorn workers.

    Sync workers are separate processes, so a thread lock alone dedupes
    nothing in production; a POSIX record lock does, and the kernel drops
    it when a worker dies. lockf does not exclude threads of one process
    (the broker's warm pool), hence the thread lock around it, which also
    keeps this process to one fd per lock file: closing any fd on the
    file would drop the record lock. Waiting is bounded by `deadline` so a
    waiter cannot outlive the request's Git budget.
    """
    budget = max(deadline - time.monotonic(), 0)
    with _walk_locks_guard:
        thread_lock = _walk_locks.setdefault(key, threading.Lock())
    if not thread_lock.acquire(timeout=budget):
        raise subprocess.TimeoutExpired("release history lock", budget)
    try:
        name = "mimir-" + key.replace(":", "-") + ".lock"
        fd = os.open(
            Path(tempfile.gettempdir()) / name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            while True:
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(
                            "release history lock", budget
                        ) from None
                    time.sleep(_LOCK_POLL_SEC)
            yield
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


def _cached_git(release: Release) -> tuple[ReleaseFacts, list[str] | None] | None:
    values = cache.get_many([release.key + ":facts", release.key + ":members"])
    facts = values.get(release.key + ":facts")
    if facts is not None and release.key + ":members" in values:
        return ReleaseFacts.model_validate(facts), values[release.key + ":members"]
    return None


def git_facts(release: Release) -> tuple[ReleaseFacts, list[str] | None]:
    """Keep membership separate so an index cache hit decodes only facts.

    A cold release is walked once however many requests ask for it: the
    rest wait for the walker and read its result from the cache, which
    a web worker's `cache.set` has committed by the time it returns.
    Waiting and walking share one GIT_TIMEOUT_SEC budget, so a request
    queued behind a walk that failed cannot spend a second full timeout
    and reach the worker timeout; it fails into the route's 503 instead.
    """
    cached = _cached_git(release)
    if cached is not None:
        return cached
    deadline = time.monotonic() + GIT_TIMEOUT_SEC
    with _single_flight(release.key, deadline):
        cached = _cached_git(release)
        if cached is not None:
            return cached
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git rev-list", GIT_TIMEOUT_SEC)
        facts, members = calculate_git(release, timeout=remaining)
        # Feature-versioned plain payloads leave every pre-existing cache
        # type and namespace valid during a rolling deployment or rollback.
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


def has_recorded_history(facts: ReleaseFacts, links: ReleaseLinks) -> bool:
    """A Git range and at least one recorded mailing-list reference.

    The sitemap and the history index both list a release by this, so
    what one advertises the other shows. One reference is the bar (#1130):
    early releases with a single reference still pass.
    """
    return facts.commit_count is not None and (links.pair_count or 0) > 0


@dataclass(frozen=True)
class RecordedHistory:
    listed: list[tuple[Release, ReleaseFacts, ReleaseLinks]]
    # Releases whose history is cached, listed or not. The rest are not
    # yet known either way and stay out until the warm calculates them.
    checked: int


def recorded_history(releases: list[Release]) -> RecordedHistory:
    """Decide from cached state only; deciding must never start a walk."""
    keys = [r.key + suffix for r in releases for suffix in (":facts", ":links")]
    values = cache.get_many(keys)
    listed = []
    checked = 0
    for release in releases:
        raw_facts = values.get(release.key + ":facts")
        raw_links = values.get(release.key + ":links")
        if raw_facts is None or raw_links is None:
            continue
        checked += 1
        facts = ReleaseFacts.model_validate(raw_facts)
        links = ReleaseLinks.model_validate(raw_links)
        if has_recorded_history(facts, links):
            listed.append((release, facts, links))
    return RecordedHistory(listed, checked)


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


_ARCHIVED_SERIES_SQL = text(
    f"""
SELECT a.id, a.patch_series_key,
       {series_head_sql("a")} AS series_head,
       {version_number_sql("a.patch_series_version")} AS series_rev
  FROM articles a
 WHERE a.message_id IN :ids
   AND a.date IS NOT NULL
   AND EXISTS (SELECT 1 FROM article_lists l WHERE l.article_id = a.id)
"""
).bindparams(bindparam("ids", expanding=True))


def calculate_subsystems(session: Session, message_ids: list[str]) -> ReleaseSubsystems:
    """Group the archived messages among `message_ids` by primary
    subsystem with the merge window's own grouping (`rank_postings`,
    `subsystem_sections`). The same archived test as `archived_page`, so
    `references` equals its total for the same IDs and the same archive.
    The page shows the cached copy, which can be up to a day older than
    the page's own total.

    Cost, production: NOT YET MEASURED (#1136 step 3). Put here v7.3, the
    largest release (14,040 distinct recorded message IDs on 2026-11-08),
    with what was counted and when: `tools/measure_release_subsystems.py`.
    The warm pass's time budget (`SUBSYSTEMS_FILL_BUDGET_SEC`) is sized
    from the figures here: revisit it when the production one is written.

    Cost, local floor (synthetic, NOT production): 2026-11-09, 14,040
    archived references, 41,818 `article_files` rows, 2,501 subsystems of
    three globs each, SQLite 3.54.1 on a laptop: 0.57 s and 34 statements
    per call (the first also builds the rules index, 0.02 s more). The
    page's own `archived_page` page 1 took 0.04 s there. The synthetic
    paths are far simpler than real ones; treat this as a ratio, about
    14 times the page's current request cost.
    """
    archived = []
    for chunk in batched(set(message_ids), 900):
        archived.extend(session.execute(_ARCHIVED_SERIES_SQL, {"ids": chunk}).all())
    postings, ranked = rank_postings(session, archived)
    rows: dict[str, list[MergeWindowRow]] = {}
    counts: dict[str, int] = {}
    for posting, members in postings.items():
        primary = primary_subsystem(ranked[posting])
        rows.setdefault(primary, [])
        counts[primary] = counts.get(primary, 0) + len(members)
    sections, smaller = subsystem_sections(rows, counts)
    return ReleaseSubsystems(
        sections=sections,
        smaller=smaller,
        smaller_patches=sum(g.patches for g in smaller),
        references=len(archived),
    )


def release_subsystems(release: Release) -> ReleaseSubsystems | None:
    """The cached grouping, or None. A request never computes it: the
    hourly warm (`warm_release_subsystems`) fills a missing one and the
    daily links refresh rewrites it, so a page without it shows no
    subsystem section until then.

    Written beside the links summary, so it is as old as the links it
    derives from. MAINTAINERS changes reach it at the next daily refresh;
    the page carries no validator, so nothing pins the older copy
    (AGENTS.md, cache validators).
    """
    raw = cache.get(release.key + ":subsystems")
    return ReleaseSubsystems.model_validate(raw) if raw is not None else None


# Time one warm pass may spend filling missing groupings. Sized from the
# local floor in `calculate_subsystems` (about 40 microseconds per
# reference, so a cold fill of ~800,000 references across every release is
# about 30 s there; production is unmeasured, assume 10x): 60 s fills the
# newest releases in the first pass and the rest over a few hourly passes.
# It is not the whole allowance: the slow tier runs as one `warm_global` op
# with a single 300 s timeout, shared with every other slow target,
# including each release's `warm_release`, whose daily links refresh also
# groups, with no budget. The fill runs last for that reason. A release
# that starts inside the budget finishes, so a pass can overrun by one.
# PRODUCTION FIGURE: NOT YET MEASURED, see `tools/measure_release_subsystems.py`.
SUBSYSTEMS_FILL_BUDGET_SEC = 60


def warm_release_subsystems(
    session: Session,
    releases: list[Release],
    budget_sec: float = SUBSYSTEMS_FILL_BUDGET_SEC,
) -> None:
    """Fill missing groupings, `releases` order (newest first) until the
    budget is spent. A cached one is left alone (the daily links refresh
    rewrites it), and a release whose ID list is not cached yet waits for
    its links. A release that fails is logged and skipped."""
    deadline = time.monotonic() + budget_sec
    for release in releases:
        if time.monotonic() >= deadline:
            return
        key = release.key + ":subsystems"
        session.rollback()
        if cache.alive(session, key):
            continue
        payload = cache.get(release.key + ":links_data")
        if payload is None:
            continue
        # One failing release must not block every older one on every pass.
        try:
            result = calculate_subsystems(session, payload["message_ids"])
            session.rollback()
            cache.set(key, result.model_dump(mode="json"), LINKS_TTL_SEC)
        except Exception:
            logger.warning(
                "release subsystems failed for %s", release.tag, exc_info=True
            )
            session.rollback()


def release_data(
    release: Release, group: bool = False
) -> tuple[ReleaseFacts, ReleaseLinks, list[str]]:
    """Compute at most one range, without holding a DB snapshot during Git.

    `group` also writes the subsystem grouping with the links. Only the
    warm asks for it: a request that finds the links cold must not run the
    grouping."""
    facts = None
    with SessionLocal() as session:

        def compute():
            nonlocal facts
            # This session belongs to this helper, not its caller. End the
            # cache lookup's transaction before walking Git or writing caches.
            session.rollback()
            facts, members = git_facts(release)
            pair_count, message_ids = calculate_links(session, members)
            if group:
                cache.set(
                    release.key + ":subsystems",
                    calculate_subsystems(session, message_ids).model_dump(mode="json"),
                    LINKS_TTL_SEC,
                )
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


class _Lapsed(Exception):
    pass


def _renew_git(session: Session, release: Release) -> None:
    """Extend cached Git facts before they expire, on every warm pass.

    Nothing else rewrites them: links refreshes read them through a cache
    hit. A cold fill writes every release in one run, so without renewal
    they expire together and every release leaves the index and the
    sitemap at once until the next walk (#1131 review). Checking on every
    pass, not only when links recompute about once a day, is what lands
    a check inside the facts' refresh window.

    The facts are fixed under their key, which carries the tag object
    IDs, so renewal rewrites the values held and never walks Git. Off
    the window `get_or_compute` only reads the small facts row; members
    are read only to be rewritten.
    """
    facts_key, members_key = release.key + ":facts", release.key + ":members"
    if not cache.alive(session, facts_key):
        # Not filled yet, or a broker write still queued: nothing to renew.
        return

    def rewrite():
        values = cache.get_many([facts_key, members_key])
        if facts_key not in values or members_key not in values:
            raise _Lapsed
        cache.set(members_key, values[members_key], GIT_TTL_SEC)
        return values[facts_key]

    try:
        cache.get_or_compute(session, facts_key, GIT_TTL_SEC, rewrite)
    except _Lapsed:
        # Expired since the check; the next walk refills it.
        pass


def warm_release(session: Session, release: Release) -> None:
    """Adapter for the slow warmer's shared, read-only target session."""
    # Earlier global targets can leave their read transaction open. This
    # adapter alone ends it; general service helpers never roll back callers.
    session.rollback()
    try:
        release_data(release, group=True)
        session.rollback()
        _renew_git(session, release)
    finally:
        session.rollback()


def archived_page(
    session: Session, message_ids: list[str], page: int
) -> tuple[list[tuple[Article, str]], int]:
    """Intersect cached IDs with today's archive; load metadata for one page.

    Lightweight ID/date batches bound SQL variables without caching authors,
    inbox ownership or thread roots. A newly archived message therefore appears
    even while the release's recorded-reference cache remains valid.

    The total counts candidates, and that equals what can be reached:
    `_advertised_urls_for` drops an article only when it has no date or
    no inbox link, and the candidate query already requires both. Every
    other article gets its thread page or, failing that, its own message
    URL (#1130). Only a row unlinked between the two queries can fall out,
    and that page is one row short for that one render.

    Every page pays for the whole release: all its IDs are matched (one
    `IN` per 900) and sorted, whatever page is asked for. Measured on
    production 2026-11-08 (3.48.0, one run each, cached links) for v7.3,
    the largest release: 14,126 recorded references, 14,040 distinct
    IDs, 14,003 archived, 281 pages. Page 1 (50 rows) took 0.189 s in 83
    statements; page 281 (3 rows) took 0.118 s in 28. The last page
    approximates the fixed whole-release part; the rest scales with the
    rows shown, mostly one `thread_page_of` COUNT per row in
    `_advertised_urls_for`. Reading and decoding the cached ID list took
    0.005 s. Re-measure before relying on these as releases grow.
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
