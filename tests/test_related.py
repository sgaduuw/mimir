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
