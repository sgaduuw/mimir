"""Related-discussions retrieval and scoring for non-patch threads.

One concern: given a non-patch thread's root and members, find up to
RELATED_LIMIT prior threads in the same inbox that look related,
using only signals already in SQLite (subject tokens, shared
participants, recency). Zero new dependencies by design. See #71 and
_claude/specs/2026-06-12-related-discussions-design.md.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from mimir import cache
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.models import Article, ArticleList, Inbox
from mimir.threading import find_thread_root

logger = logging.getLogger(__name__)

# Dedicated logger for the #71 instrumentation line. The general
# application loggers (mimir.*) have NO INFO handler in the gunicorn
# web tier, so a plain `logger.info(...)` is silently dropped in
# production (caught 2026-06-13: the panel renders but no
# `related-discussions:` line ever reached the container log). Mirror
# the proven `mimir.request` access-log logger in `mimir/web/hooks.py`:
# own StreamHandler at INFO, `propagate = False` so it emits regardless
# of root config and never double-logs through a reconfigured root.
_metric_logger = logging.getLogger("mimir.related.metric")
_metric_logger.propagate = False
if not _metric_logger.handlers:
    _mh = logging.StreamHandler()
    _mh.setFormatter(logging.Formatter("%(message)s"))
    _metric_logger.addHandler(_mh)
    _metric_logger.setLevel(logging.INFO)

# Panel and scoring tuning knobs. Module constants, not Settings:
# they are scoring calibration, not per-deploy config. The candidate
# window IS per-deploy and lives on Settings
# (related_discussions_window_days).
RELATED_LIMIT = 5
CANDIDATE_CAP = 100
SCORE_EXACT_SUBJECT = 6.0
SCORE_PER_TOKEN = 3.0
SCORE_PER_PARTICIPANT = 2.0
PARTICIPANT_CAP = 3
SCORE_THRESHOLD = 2.0
DECAY_HALF_LIFE_DAYS = 180.0
CACHE_TTL = 3600

# ASCII-only by design: lkml subjects are ASCII in practice; non-ASCII chars split (accented names degrade to fragments, acceptable for subject matching).
_TOKEN_SPLIT = re.compile(r"[^a-z0-9_]+")
_BRACKETED = re.compile(r"\[[^\]]*\]")
# English function words common in subject lines plus lkml noise
# words that match everything and discriminate nothing.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "when",
        "after",
        "before",
        "while",
        "does",
        "what",
        "have",
        "has",
        "are",
        "is",
        "not",
        "can",
        "cannot",
        "into",
        "about",
        "over",
        "under",
        "between",
        "during",
        "kernel",
        "linux",
        "regression",
        "question",
        "problem",
        "issue",
        "error",
        "failed",
        "failure",
        "help",
        "patch",
        "warning",
    }
)


@dataclass(frozen=True)
class RelatedThread:
    """One related prior thread, render-ready.

    `year`/`month` come from the root article's date (the URL
    identity rule: /<inbox>/<YYYY>/<MM>/<id> must match exactly or
    the route 404s); 0/0 when the root has no date, matching the
    thread_urls fallback in the message view. `signals` is the
    subset of ("subject", "token", "participant") that matched;
    rendered as a tooltip, also the strong/weak classifier input.
    """

    article_id: int
    inbox_name: str
    year: int
    month: int
    subject: str | None
    last_activity: datetime | None
    score: float
    signals: tuple[str, ...]


cache.register("related_thread", RelatedThread)


def _score_and_classify(
    *,
    exact_subject: bool,
    matched_tokens: set[str],
    shared_authors: set[str],
    last_activity: datetime | None,
    now: datetime,
) -> tuple[float, float, tuple[str, ...]]:
    """Score one candidate root and label its matched signals.

    Returns (base, decayed, signals). The THRESHOLD cut applies to
    `base` (signal quality), while ordering uses `decayed` (recency
    as tie-break across the window): a strong old match should still
    render, just below an equally strong fresh one. Spec records the
    threshold as 2.0 on the signal score.
    """
    base = 0.0
    signals: list[str] = []
    if exact_subject:
        base += SCORE_EXACT_SUBJECT
        signals.append("subject")
    if matched_tokens:
        base += SCORE_PER_TOKEN * len(matched_tokens)
        signals.append("token")
    if shared_authors:
        base += SCORE_PER_PARTICIPANT * min(len(shared_authors), PARTICIPANT_CAP)
        signals.append("participant")
    decayed = base
    if last_activity is not None:
        age_days = max((now - last_activity).total_seconds() / 86400.0, 0.0)
        decayed = base * (0.5 ** (age_days / DECAY_HALF_LIFE_DAYS))
    return base, decayed, tuple(signals)


def _rare_tokens(subject: str | None, top: int = 3) -> list[str]:
    """Distinctive tokens of a subject line, longest first.

    Lowercase, strip bracketed tags ([RFC], [BUG], ...), split on
    anything outside [a-z0-9_] (underscores survive so kernel identifiers
    like spin_lock_irqsave stay one token), drop stopwords and tokens shorter than 4
    chars, rank by descending length (longer is rarer in subject
    lines), alphabetical tie-break for determinism, take `top`.
    """
    if not subject:
        return []
    cleaned = _BRACKETED.sub(" ", subject.lower())
    tokens = {
        t for t in _TOKEN_SPLIT.split(cleaned) if len(t) >= 4 and t not in _STOPWORDS
    }
    return sorted(tokens, key=lambda t: (-len(t), t))[:top]


def is_bot_sender(author: str | None) -> bool:
    """True when `author` matches a configured bot/automated sender.

    Substring match (case-insensitive) against
    `settings.related_discussions_bot_senders`. Used to drop bot
    threads from related-discussions candidates and to suppress the
    panel on bot-authored roots (#71): syzbot, the kernel test robot,
    and tip-bot dominate the non-patch population and produce
    low-value matches.
    """
    if not author:
        return False
    low = author.casefold()
    return any(
        tok.casefold() in low for tok in settings.related_discussions_bot_senders
    )


def _candidate_select(
    inbox_id: int,
    *,
    subject_normalized: str,
    tokens: list[str],
    authors: set[str],
    min_date: datetime,
) -> Select | None:
    """The candidate query. One walk of ix_articles_date descending
    with LIMIT; the spec's three branches are OR predicates tested
    per row during the walk. Returns None when no predicate applies
    (empty subject, no tokens, no authors). Exposed separately from
    `_candidates` so the plan-pin test can EXPLAIN the compiled SQL.

    Tokens may contain `_`, which LIKE treats as a single-char
    wildcard (so %spin_lock% also matches spinXlock). Accepted: the
    only caller feeds [a-z0-9_] tokens from _rare_tokens and the
    widening is negligible for subject matching.
    """
    preds = []
    if subject_normalized:
        preds.append(Article.subject_normalized == subject_normalized)
    for t in tokens:
        preds.append(Article.subject_normalized.like(f"%{t}%"))
    if authors:
        preds.append(Article.author.in_(sorted(authors)))
    if not preds:
        return None
    return (
        select(
            Article.id,
            Article.message_id,
            Article.subject_normalized,
            Article.author,
            Article.date,
        )
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox_id,
            Article.date >= min_date,
            or_(*preds),
        )
        .order_by(Article.date.desc())
        .limit(CANDIDATE_CAP)
    )


def _candidates(
    session: Session,
    inbox: Inbox,
    *,
    exclude_ids: set[int],
    subject_normalized: str,
    tokens: list[str],
    authors: set[str],
    min_date: datetime,
) -> list:
    """Execute the candidate query and drop in-thread rows.

    Note: `exclude_ids` filters in Python AFTER the SQL LIMIT, so a
    thread whose own messages dominate the most-recent-100 matches
    yields fewer (possibly zero) candidates even when older
    out-of-thread matches exist. Deliberate capacity trade: the
    exclude set stays out of the SQL (no unbounded IN list) and the
    cap reads as "the 100 most recent matching messages".
    """
    stmt = _candidate_select(
        inbox.id,
        subject_normalized=subject_normalized,
        tokens=tokens,
        authors=authors,
        min_date=min_date,
    )
    if stmt is None:
        return []
    return [r for r in session.execute(stmt).all() if r.id not in exclude_ids]


def related_discussions(
    session: Session,
    inbox: Inbox,
    *,
    root_id: int,
    thread_article_ids: set[int],
    thread_authors: set[str],
) -> list[RelatedThread]:
    """Up to RELATED_LIMIT related prior threads for a non-patch
    thread, cached per root. The cold path runs the candidate query,
    collapses candidates to their thread roots, scores per root, and
    emits the instrumentation line the #71 decision rule reads.
    """

    def _compute() -> list[RelatedThread]:
        t0 = time.perf_counter()
        now = datetime.now(timezone.utc)
        root = session.get(Article, root_id)
        if root is None:
            return []
        tokens = _rare_tokens(root.subject_normalized)
        # Bot/automated senders (syzbot, kernel test robot, tip-bot)
        # dominate the non-patch population and produce low-value
        # matches (#71). Drop them from the authors used for candidate
        # generation and participant scoring so a bot co-author never
        # drags in every thread it touched.
        human_authors = {a for a in thread_authors if a and not is_bot_sender(a)}
        rows = _candidates(
            session,
            inbox,
            exclude_ids=thread_article_ids,
            subject_normalized=root.subject_normalized or "",
            tokens=tokens,
            authors=human_authors,
            min_date=now - timedelta(days=settings.related_discussions_window_days),
        )

        # Collapse candidate messages onto their thread roots and
        # aggregate per-root evidence. find_thread_root is ~2-3 ms
        # per walk; CANDIDATE_CAP bounds the total.
        authors_cf = {a.casefold().strip() for a in human_authors}
        token_set = set(tokens)
        evidence: dict[str, dict] = {}
        for r in rows:
            root_mid = find_thread_root(session, inbox, r.message_id)
            if root_mid is None:
                continue
            ev = evidence.setdefault(
                root_mid,
                {
                    "exact": False,
                    "tokens": set(),
                    "authors": set(),
                    "last": None,
                },
            )
            if (
                root.subject_normalized
                and r.subject_normalized == root.subject_normalized
            ):
                ev["exact"] = True
            if r.subject_normalized:
                ev["tokens"] |= {t for t in token_set if t in r.subject_normalized}
            if r.author and r.author.casefold().strip() in authors_cf:
                ev["authors"].add(r.author.casefold().strip())
            if r.date and (ev["last"] is None or r.date > ev["last"]):
                ev["last"] = r.date

        # Fetch the root Article rows for surviving roots; drop the
        # current thread if a candidate walked back into it, and drop
        # threads whose root author is a bot (e.g. other syzbot
        # reports) wholesale, regardless of which reply matched.
        # Note: bot rows are filtered here at the root, not in the SQL
        # `_candidates` query, so they still consume CANDIDATE_CAP. On
        # an inbox where bot threads dominate a token's matches this
        # can crowd out older human candidates; `bot_filtered` in the
        # log line surfaces the volume so #71's review can judge it.
        root_arts = {}
        bot_filtered = 0
        if evidence:
            for a in session.execute(
                select(Article).where(Article.message_id.in_(list(evidence.keys())))
            ).scalars():
                if a.id == root_id or a.id in thread_article_ids:
                    continue
                if is_bot_sender(a.author):
                    bot_filtered += 1
                    continue
                root_arts[a.message_id] = a

        scored: list[tuple[float, RelatedThread]] = []
        for mid, art in root_arts.items():
            ev = evidence[mid]
            # Article.date from SQLite arrives tz-naive; normalize to
            # aware UTC so the decay subtraction doesn't raise.
            last_activity = aware_utc(ev["last"]) if ev["last"] else None
            base, decayed, signals = _score_and_classify(
                exact_subject=ev["exact"],
                matched_tokens=ev["tokens"],
                shared_authors=ev["authors"],
                last_activity=last_activity,
                now=now,
            )
            if base < SCORE_THRESHOLD:
                continue
            scored.append(
                (
                    decayed,
                    RelatedThread(
                        article_id=art.id,
                        inbox_name=inbox.name,
                        year=art.date.year if art.date else 0,
                        month=art.date.month if art.date else 0,
                        subject=art.subject,
                        last_activity=last_activity,
                        score=round(decayed, 2),
                        signals=signals,
                    ),
                )
            )
        scored.sort(key=lambda p: -p[0])
        result = [item for _, item in scored[:RELATED_LIMIT]]

        strong = sum(
            1 for item in result if "subject" in item.signals or "token" in item.signals
        )
        _metric_logger.info(
            "related-discussions: inbox=%s root=%d candidates=%d "
            "rendered=%d strong=%d weak=%d bot_filtered=%d top=%.1f "
            "elapsed_ms=%d",
            inbox.name,
            root_id,
            len(rows),
            len(result),
            strong,
            len(result) - strong,
            bot_filtered,
            result[0].score if result else 0.0,
            int((time.perf_counter() - t0) * 1000),
        )
        return result

    return cache.get_or_compute(
        session,
        f"related_discussions:{inbox.name}:{root_id}",
        CACHE_TTL,
        _compute,
    )
