"""Tests for mimir/related.py: related-discussions retrieval and
scoring for non-patch threads (#71).

Note: datetime/timedelta/timezone and pytest imports are added back
by later tasks (scoring, recency decay, parametrize). Removed here
to satisfy ruff F401; later tasks re-add what they need.
"""


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
