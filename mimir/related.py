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

from mimir import cache

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
