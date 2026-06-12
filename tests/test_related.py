"""Tests for mimir/related.py: related-discussions retrieval and
scoring for non-patch threads (#71).
"""

import pytest
from datetime import datetime, timedelta, timezone


class TestRareTokens:
    def test_picks_longest_distinctive_tokens(self):
        from mimir.related import _rare_tokens

        out = _rare_tokens("bcachefs deadlock during journal replay")
        # Stopword-free tokens ranked by descending length, top 3.
        assert out == ["bcachefs", "deadlock", "journal"]

    def test_strips_bracketed_tags_and_stopwords(self):
        from mimir.related import _rare_tokens

        out = _rare_tokens("[RFC] [BUG] the kernel question about lockdep")
        assert out == ["lockdep"]

    def test_drops_short_tokens(self):
        from mimir.related import _rare_tokens

        assert _rare_tokens("rcu fix v2") == []

    def test_empty_and_none_subjects(self):
        from mimir.related import _rare_tokens

        assert _rare_tokens(None) == []
        assert _rare_tokens("") == []

    def test_deterministic_tiebreak_on_equal_length(self):
        from mimir.related import _rare_tokens

        # Same length tokens tie-break alphabetically so the cache
        # key inputs are stable across runs.
        out = _rare_tokens("zzzz aaaa cccc bbbb")
        assert out == ["aaaa", "bbbb", "cccc"]


class TestRelatedThreadCacheRoundTrip:
    def test_encode_decode_preserves_fields(self):
        """RelatedThread must survive the cache JSON round-trip
        (cache.register convention; cache knows nothing about its
        callers, each module registers its own dataclasses)."""
        from mimir import cache
        from mimir.related import RelatedThread

        original = RelatedThread(
            article_id=42,
            inbox_name="alpha",
            year=2026,
            month=6,
            subject="bcachefs journal deadlock",
            last_activity=datetime(2026, 6, 1, tzinfo=timezone.utc),
            score=8.25,
            signals=("token", "participant"),
        )
        import json

        decoded = cache._decode(json.loads(json.dumps(cache._encode([original]))))
        assert decoded == [original]


class TestScoreAndClassify:
    NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

    def _call(self, **kw):
        from mimir.related import _score_and_classify

        defaults = dict(
            exact_subject=False,
            matched_tokens=set(),
            shared_authors=set(),
            last_activity=self.NOW,
            now=self.NOW,
        )
        defaults.update(kw)
        return _score_and_classify(**defaults)

    def test_exact_subject_scores_and_labels(self):
        base, decayed, signals = self._call(exact_subject=True)
        assert base == 6.0
        assert decayed == 6.0  # zero age, no decay
        assert signals == ("subject",)

    def test_tokens_and_participants_accumulate(self):
        base, decayed, signals = self._call(
            matched_tokens={"bcachefs", "journal"},
            shared_authors={"a", "b"},
        )
        assert base == 3.0 * 2 + 2.0 * 2
        assert signals == ("token", "participant")

    def test_participant_overlap_capped_at_three(self):
        base, _, _ = self._call(shared_authors={"a", "b", "c", "d", "e"})
        assert base == 2.0 * 3

    def test_decay_halves_at_half_life(self):
        from mimir.related import DECAY_HALF_LIFE_DAYS

        base, decayed, _ = self._call(
            exact_subject=True,
            last_activity=self.NOW - timedelta(days=DECAY_HALF_LIFE_DAYS),
        )
        assert base == 6.0
        assert decayed == pytest.approx(3.0)

    def test_none_last_activity_skips_decay(self):
        base, decayed, _ = self._call(exact_subject=True, last_activity=None)
        assert decayed == base == 6.0

    def test_strong_vs_weak_signal_sets(self):
        _, _, strong = self._call(matched_tokens={"lockdep"})
        _, _, weak = self._call(shared_authors={"a"})
        assert "token" in strong
        assert weak == ("participant",)


def _seed_message(
    s,
    inbox,
    message_id: str,
    subject: str,
    author: str,
    days_ago: int,
    thread_parent: str | None = None,
):
    """One Article + ArticleList row in `inbox`. Relative date so
    window queries stay valid as wall-clock advances."""
    from mimir.models import Article, ArticleList
    from mimir.parser import normalize_subject

    art = Article(
        message_id=message_id,
        subject=subject,
        author=author,
        date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        thread_parent=thread_parent,
        subject_normalized=normalize_subject(subject),
    )
    s.add(art)
    s.flush()
    s.add(
        ArticleList(
            article_id=art.id,
            inbox_id=inbox.id,
            epoch="0.git",
            commit_sha=format(art.id, "x").rjust(40, "0"),
        )
    )
    return art


class TestCandidates:
    def test_three_predicate_families_and_exclusion(self, seeded_db):
        from sqlalchemy import select as sa_select

        from mimir.models import Inbox
        from mimir.related import _candidates

        with seeded_db() as s:
            alpha = s.execute(
                sa_select(Inbox).where(Inbox.name == "alpha")
            ).scalar_one()
            by_subject = _seed_message(
                s, alpha, "c1@x", "bcachefs journal deadlock", "Alice", 30
            )
            by_token = _seed_message(
                s, alpha, "c2@x", "fix journal replay hang", "Bob", 40
            )
            by_author = _seed_message(
                s, alpha, "c3@x", "totally unrelated words", "Carol", 50
            )
            too_old = _seed_message(
                s, alpha, "c4@x", "bcachefs journal deadlock", "Dave", 4000
            )
            excluded = _seed_message(
                s, alpha, "c5@x", "bcachefs journal deadlock", "Eve", 10
            )
            s.commit()

            rows = _candidates(
                s,
                alpha,
                exclude_ids={excluded.id},
                subject_normalized="bcachefs journal deadlock",
                tokens=["journal"],
                authors={"Carol"},
                min_date=datetime.now(timezone.utc) - timedelta(days=365),
            )
            got_ids = {r.id for r in rows}
            assert by_subject.id in got_ids  # exact subject_normalized
            assert by_token.id in got_ids  # rare-token LIKE
            assert by_author.id in got_ids  # shared author
            assert too_old.id not in got_ids  # outside window
            assert excluded.id not in got_ids  # in-thread exclusion

    def test_no_predicates_returns_empty(self, seeded_db):
        from sqlalchemy import select as sa_select

        from mimir.models import Inbox
        from mimir.related import _candidates

        with seeded_db() as s:
            alpha = s.execute(
                sa_select(Inbox).where(Inbox.name == "alpha")
            ).scalar_one()
            assert (
                _candidates(
                    s,
                    alpha,
                    exclude_ids=set(),
                    subject_normalized="",
                    tokens=[],
                    authors=set(),
                    min_date=datetime.now(timezone.utc),
                )
                == []
            )
