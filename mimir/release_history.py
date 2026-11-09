"""Closed Linux releases: immutable Git ranges and pull requests, and
expiring recorded links."""

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
from datetime import UTC, datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import NamedTuple

from dulwich.errors import NotGitRepository
from dulwich.objects import Commit, Tag
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.datetime_utils import aware_utc
from mimir.extensions import SessionLocal
from mimir.linus_pulls import ClosedPullState, Pull, classify
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


class ReleasePullSummary(BaseModel):
    """Counts over a release's classified pull requests, the small row the
    history index and the sitemap read (`has_recorded_history`)."""

    count: int
    unclear: int
    # Pulls whose body names its last commit ("up to <sha>").
    tips: int
    # Pulls pr-tracker-bot replied were merged, inside vPREV..vN.
    bot_replies: int


class ReleasePull(NamedTuple):
    """One row as the shared cards render it, resolved at render time."""

    subject: str | None
    url: str
    author: str | None
    date: datetime | None
    state: ClosedPullState


@dataclass(frozen=True)
class ReleasePulls:
    summary: ReleasePullSummary
    rows: list[ReleasePull]


# A release's pull requests, as two immutable rows like `:facts` and
# `:members`: the summary the index reads, and `[[article id, state], ...]`
# newest first. Both live GIT_TTL_SEC and every warm pass renews them
# (`_renew_immutable`); nothing recalculates them. Mail ingested later for
# an old window, or a change to the classifier, does not reach a cached
# release until this version suffix is bumped on purpose.
PULLS = ":pulls:1"
PULL_ROWS = ":pull_rows:1"
# Merge-window pull requests are often sent in vPREV's last rc week, before
# vPREV exists, so candidates start this long before it. #1134 measured 16
# to 69 pulls per release in this lead-in that were not merged into vN;
# they belong to vPREV only if they made it in or Linus rejected them. The same
# span at vN's end is vN+1's lead-in (`_in_cycle`).
PULLS_LEAD_IN = timedelta(days=14)
# The fill classifies a release only once its tag is this old, so replies
# dated by the tag that are ingested a little later are there when its
# immutable rows are written. A judgement, not a measurement: mail reaches
# the archive within minutes to hours.
PULLS_SETTLE = timedelta(days=1)
# Past this share of Unclear pulls the page says how many name no commit.
# #1134: under 6% from v5.0 on (5.3% for v5.0), 20.8% for v4.20, the first
# window with pr-tracker-bot replies, and 40% to 100% before it.
UNCLEAR_NOTE_SHARE = 0.10


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


def has_recorded_history(
    facts: ReleaseFacts, links: ReleaseLinks, pulls: ReleasePullSummary | None
) -> bool:
    """A Git range and (at least one recorded mailing-list reference or at
    least one classified pull request, Unclear included, #1132).

    The sitemap and the history index both list a release by this, so
    what one advertises the other shows. One reference is the bar (#1130):
    early releases with a single reference still pass. `pulls` is None
    until the warm has classified the release.
    """
    return facts.commit_count is not None and (
        (links.pair_count or 0) > 0 or (pulls is not None and pulls.count > 0)
    )


@dataclass(frozen=True)
class RecordedHistory:
    listed: list[tuple[Release, ReleaseFacts, ReleaseLinks]]
    # Releases whose history is cached, listed or not. The rest are not
    # yet known either way and stay out until the warm calculates them.
    checked: int


def recorded_history(releases: list[Release]) -> RecordedHistory:
    """Decide from cached state only; deciding must never start a walk.

    A release is checked once the decision is known: listed, without a
    Git range, or with its pull requests classified. One with no recorded
    reference and no pulls yet may still be listed when they arrive."""
    keys = [r.key + suffix for r in releases for suffix in (":facts", ":links", PULLS)]
    values = cache.get_many(keys)
    listed = []
    checked = 0
    for release in releases:
        raw_facts = values.get(release.key + ":facts")
        raw_links = values.get(release.key + ":links")
        if raw_facts is None or raw_links is None:
            continue
        facts = ReleaseFacts.model_validate(raw_facts)
        links = ReleaseLinks.model_validate(raw_links)
        raw_pulls = values.get(release.key + PULLS)
        pulls = ReleasePullSummary.model_validate(raw_pulls) if raw_pulls else None
        if has_recorded_history(facts, links, pulls):
            listed.append((release, facts, links))
        elif facts.commit_count is not None and pulls is None:
            continue
        checked += 1
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

    Cost, measured on production, 2026-11-09 (3.48.0, read-only, cache
    writes blocked, `tools/measure_release_subsystems.py`): v7.3, the
    largest release, with 14,040 cached message IDs, 14,003 archived
    references, 7,235 postings, 118 sections and 890 smaller subsystems
    holding 4,992. Three runs took 0.769, 0.747 and 0.744 s, 34 statements
    each, about 53 microseconds per reference. `archived_page` page 1 on
    the same release took 0.156 s in 83 statements. All 114 releases had
    a cached ID list, 313,565 distinct IDs in total, so grouping every
    release once costs about 17 s.

    Cost, local floor (synthetic, NOT production): 2026-11-09, 14,040
    archived references, 41,818 `article_files` rows, 2,501 subsystems of
    three globs each, SQLite 3.54.1 on a laptop: 0.57 s and 34 statements
    per call (the first also builds the rules index, 0.02 s more). The
    page's own `archived_page` page 1 took 0.04 s there. The synthetic
    paths are far simpler than real ones: production came out at 1.3
    times this floor.
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


# Time one warm pass may spend filling missing groupings. Measured on
# production 2026-11-09 (see `calculate_subsystems`): about 53 microseconds
# per reference and 313,565 distinct IDs across all 114 releases, so a cold
# fill of every release takes about 17 s and 60 s fills them all in one
# pass. It is not the whole allowance: the slow tier runs as one
# `warm_global` op with a single 300 s timeout, shared with every other slow
# target, including each release's `warm_release`, whose daily links refresh
# also groups, with no budget (regrouping every release at once adds about
# 17 s to a slow warm_global that took 3 to 10 s in 17 hourly runs on
# 2026-11-08/09). The fill runs last for that reason. A release that starts
# inside the budget finishes, so a pass can overrun by one.
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


def calculate_pulls(
    session: Session,
    release: Release,
    members: list[str],
    previous_members: list[str] | None,
) -> tuple[ReleasePullSummary, list[list]] | None:
    """Classify the pull requests to Linus of `release`'s cycle with
    `/merge-window`'s classifier (`linus_pulls.classify`), or None when a
    tag date is missing.

    Candidates are sent from PULLS_LEAD_IN before vPREV to vN, tag dates
    as the page shows them. Merged: the named tip is in `members` (vN's
    range, a set lookup, no walk), or pr-tracker-bot replied it was merged
    after vPREV and by vN. The rest are rejected, not merged in vN (a tip
    outside the range) or unclear (no tip and no bot reply).

    Window rule: `_in_cycle`.

    Cost: measured beside `PULLS_FILL_BUDGET_SEC`.
    """
    with Repo(str(release.path)) as repo:
        _, start, _ = _tag_metadata(repo, release.previous_oid)
        _, end, _ = _tag_metadata(repo, release.oid)
    if start is None or end is None:
        return None
    merged = set(members)
    previous = set(previous_members) if previous_members is not None else None
    pulls = [
        p
        for p in classify(
            session,
            start - PULLS_LEAD_IN,
            end,
            merged,
            closed=True,
            bot_window=(start, end),
        )
        if _in_cycle(p, start, end, merged, previous)
    ]
    summary = ReleasePullSummary(
        count=len(pulls),
        unclear=sum(p.state == "unknown" for p in pulls),
        tips=sum(p.tip is not None for p in pulls),
        bot_replies=sum(
            p.bot_at is not None and start < p.bot_at <= end for p in pulls
        ),
    )
    return summary, [[p.article.id, p.state] for p in pulls]


def _refused_by(pull: Pull, tagged: datetime) -> bool:
    """Linus refused it by the tag. Rows are written once, at different
    times, so a refusal sent after a tag must not change which release a
    pull belongs to: vN's row would not see it, vN+1's would."""
    return pull.rejected_at is not None and pull.rejected_at <= tagged


def _made_it(pull: Pull, members: set[str], tagged: datetime) -> bool:
    """Whether `pull` made it into the release tagged at `tagged` with
    range `members`: its tip is in the range, or pr-tracker-bot replied it
    was merged by the tag (a re-pushed tag, or a tip-less pull)."""
    return pull.tip in members or (pull.bot_at is not None and pull.bot_at <= tagged)


def _in_cycle(
    pull: Pull,
    start: datetime,
    end: datetime,
    members: set[str],
    previous_members: set[str] | None,
) -> bool:
    """Whether a classified pull belongs to vPREV..vN's page. The window
    rule's one place (decided 2026-11-09, #1135).

    Every pull sent in the PULLS_LEAD_IN overlap before a tag T, both ends
    included, belongs to exactly one of the release tagged T and the next
    one: the release tagged T if it made it in or Linus rejected it by the
    tag, otherwise the next one, with the next one's state (Rejected, if he
    refused it later). Both releases decide with `_made_it` and
    `_refused_by`, never with the state, where merged wins, so they agree
    whenever each row was written. Overlap pulls are early merge-window pulls for the next
    release more often than late fixes.

    `previous_members` None: vPREV has no range and no page, so vN keeps
    its whole lead-in.
    """
    sent = aware_utc(pull.article.date)
    if sent >= end - PULLS_LEAD_IN:
        return _refused_by(pull, end) or _made_it(pull, members, end)
    if sent <= start and previous_members is not None:
        return not (_refused_by(pull, start) or _made_it(pull, previous_members, start))
    return True


# Time one warm pass may spend classifying releases' pull requests.
#
# Cost, measured on production 2026-11-09 (`tools/measure_release_pulls.py`
# at 8acb7f7, read-only, cache writes blocked), `calculate_pulls` per
# release, runs in seconds:
#
#   v7.3    20,260 commits  2.404 / 1.928 / 1.938 s  310 statements
#           478 pulls: 476 merged, 1 rejected, 1 unclear; 404 tips,
#           472 bot replies
#   v7.2    17,833 commits  2.329 / 1.823 / 1.913 s  594 statements
#           454 pulls: 450 merged, 2 not merged, 2 unclear
#   v4.20   2.031 s, 192 statements; 335 pulls: 255 merged, 76 unclear,
#           3 not merged, 1 rejected; 151 bot replies
#   v3.0    1.015 s, 184 statements; 273 pulls: 271 unclear, 2 rejected;
#           no tips, no bot replies
#   v2.6.30 0.714 s, 138 statements; 247 pulls: 244 unclear, 3 rejected
#
# So 0.7 to 2.4 s a release, and a cold fill of all 114 releases about 150
# to 200 s: the budget spreads it over three or four hourly passes, newest
# first. Merged counts against `git log --merges --first-parent` merges
# naming a remote: 476 of 506 for v7.3, 450 of 480 for v7.2 (94% each; the
# tool's docstring says why they need not match exactly). What a page view
# pays to resolve the cached rows (`release_pulls`) was measured only
# locally, on synthetic data: 0.005 s and 3 statements for 853 rows.
#
# The budget shares the slow tier's single 300 s `warm_global` timeout with
# the subsystem fill's 60 s and the other slow targets (3 to 10 s), so the
# fill runs last; once filled, a pass only renews. A release that starts
# inside the budget finishes, so a pass can overrun by one, about 2.4 s.
PULLS_FILL_BUDGET_SEC = 60


def warm_release_pulls(
    session: Session,
    releases: list[Release],
    budget_sec: float = PULLS_FILL_BUDGET_SEC,
) -> None:
    """Classify releases whose pulls are not cached, `releases` order
    (newest first), until the budget is spent; the next pass picks up the
    rest. A cached one is left alone (`warm_release` renews it). A release
    tagged less than PULLS_SETTLE ago waits; one whose Git facts, or whose
    predecessor's, are not cached yet waits for them (`releases` must hold the predecessor), one without a Git range
    has nothing to classify, and one that fails is logged and skipped."""
    deadline = time.monotonic() + budget_sec
    by_tag = {release.tag: release for release in releases}
    for release in releases:
        if time.monotonic() >= deadline:
            return
        session.rollback()
        if cache.alive(session, release.key + PULLS):
            continue
        cached = _cached_git(release)
        if cached is None or cached[1] is None:
            continue
        tagged = cached[0].release_date
        # The cache's clock, which tests set.
        now = datetime.fromtimestamp(cache._now(), UTC)
        if tagged is None or tagged + PULLS_SETTLE > now:
            continue
        previous_members = None
        if release.previous_tag is not None:
            previous = by_tag.get(release.previous_tag)
            previous_git = _cached_git(previous) if previous else None
            if previous_git is None:
                continue
            previous_members = previous_git[1]
        # One failing release must not block every older one on every pass.
        try:
            result = calculate_pulls(session, release, cached[1], previous_members)
            session.rollback()
            if result is None:
                continue
            summary, rows = result
            # Rows first: a summary implies its rows are there.
            cache.set(release.key + PULL_ROWS, rows, GIT_TTL_SEC)
            cache.set(release.key + PULLS, summary.model_dump(mode="json"), GIT_TTL_SEC)
        except Exception:
            logger.warning("release pulls failed for %s", release.tag, exc_info=True)
            session.rollback()


def release_pulls(session: Session, release: Release) -> ReleasePulls | None:
    """The cached pull requests, resolved for display, or None. A request
    never classifies and never reads a mail: until the warm has filled
    the release, its page has no pull-request section.

    The cached rows hold article IDs and states only, so the subject,
    author, date and canonical inbox are today's, as on the archived
    messages list. A row whose article is gone or unlinked is skipped."""
    from mimir.web.urls import _canonical_inbox_names_for, _msg_path

    values = cache.get_many([release.key + PULLS, release.key + PULL_ROWS])
    if release.key + PULLS not in values or release.key + PULL_ROWS not in values:
        return None
    stored = values[release.key + PULL_ROWS]
    ids = [article_id for article_id, _ in stored]
    articles = {}
    for chunk in batched(ids, 900):
        articles.update(
            (row.id, row)
            for row in session.execute(
                select(Article.id, Article.subject, Article.author, Article.date).where(
                    Article.id.in_(chunk)
                )
            )
        )
    names = _canonical_inbox_names_for(session, list(articles))
    rows = [
        ReleasePull(
            subject=art.subject,
            url=_msg_path(art.id, art.date, names[art.id]),
            author=art.author,
            date=art.date,
            state=state,
        )
        for article_id, state in stored
        if (art := articles.get(article_id)) is not None and article_id in names
    ]
    summary = ReleasePullSummary.model_validate(values[release.key + PULLS])
    return ReleasePulls(summary, rows)


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


# Rows fixed under the release key, which carries the tag object IDs, each
# group led by the row whose expiry decides when the group is rewritten.
_IMMUTABLE_ROWS = ((":facts", ":members"), (PULLS, PULL_ROWS))


def _renew_immutable(session: Session, release: Release) -> None:
    """Extend each group of immutable rows before it expires, on every
    warm pass.

    Nothing else rewrites them: links refreshes read the facts through a
    cache hit, and the pull fill skips a release whose pulls are cached. A
    cold fill writes every release in one run, so without renewal they
    expire together and every release leaves the index and the sitemap at
    once until the next walk (#1131 review). Checking on every pass, not
    only when links recompute about once a day, is what lands a check
    inside the rows' refresh window.

    Renewal rewrites the values held and never walks Git or reads mail.
    Off the window `get_or_compute` only reads the small leading row; the
    others are read only to be rewritten.
    """
    for suffixes in _IMMUTABLE_ROWS:
        lead, *rest = [release.key + suffix for suffix in suffixes]
        if not cache.alive(session, lead):
            # Not filled yet, or a broker write still queued: nothing to renew.
            continue

        def rewrite(lead=lead, rest=rest):
            values = cache.get_many([lead, *rest])
            if any(key not in values for key in (lead, *rest)):
                raise _Lapsed
            for key in rest:
                cache.set(key, values[key], GIT_TTL_SEC)
            return values[lead]

        try:
            cache.get_or_compute(session, lead, GIT_TTL_SEC, rewrite)
        except _Lapsed:
            # Expired since the check; the next walk or fill refills it.
            pass


def warm_release(session: Session, release: Release) -> None:
    """Adapter for the slow warmer's shared, read-only target session."""
    # Earlier global targets can leave their read transaction open. This
    # adapter alone ends it; general service helpers never roll back callers.
    session.rollback()
    try:
        release_data(release, group=True)
        session.rollback()
        _renew_immutable(session, release)
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
