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
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import Article, ArticleList, Inbox

logger = logging.getLogger(__name__)

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
