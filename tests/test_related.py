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

    def test_exact_subject_predicate_isolated(self, seeded_db):
        """The equality branch must match on its own (no tokens, no
        authors), otherwise removing it would silently degrade exact
        rematches to token luck."""
        from sqlalchemy import select as sa_select

        from mimir.models import Inbox
        from mimir.related import _candidates

        with seeded_db() as s:
            alpha = s.execute(
                sa_select(Inbox).where(Inbox.name == "alpha")
            ).scalar_one()
            hit = _seed_message(
                s, alpha, "iso1@x", "bcachefs deadlock fix", "Alice", 20
            )
            miss = _seed_message(
                s, alpha, "iso2@x", "unrelated subject line", "Bob", 20
            )
            s.commit()
            rows = _candidates(
                s,
                alpha,
                exclude_ids=set(),
                subject_normalized="bcachefs deadlock fix",
                tokens=[],
                authors=set(),
                min_date=datetime.now(timezone.utc) - timedelta(days=365),
            )
            ids = {r.id for r in rows}
            assert hit.id in ids
            assert miss.id not in ids


class TestRelatedDiscussions:
    def _setup_corpus(self, s, alpha):
        """Current thread: non-patch root + one reply. Prior corpus:
        a token-related thread, a participant-related thread, an
        unrelated thread, all in alpha."""
        root = _seed_message(
            s,
            alpha,
            "cur-root@x",
            "bcachefs deadlock during journal replay",
            "Alice",
            2,
        )
        reply = _seed_message(
            s,
            alpha,
            "cur-reply@x",
            "Re: bcachefs deadlock during journal replay",
            "Bob",
            1,
            thread_parent="cur-root@x",
        )
        tok_root = _seed_message(
            s,
            alpha,
            "tok-root@x",
            "journal replay hangs on dirty mount",
            "Carol",
            60,
        )
        part_root = _seed_message(
            s,
            alpha,
            "part-root@x",
            "weekly status report thing",
            "Alice",
            90,
        )
        unrelated = _seed_message(
            s,
            alpha,
            "unrel-root@x",
            "random words entirely different",
            "Zoe",
            30,
        )
        s.commit()
        return root, reply, tok_root, part_root, unrelated

    def test_finds_scores_orders_and_logs(self, seeded_db, caplog):
        import logging

        from sqlalchemy import select as sa_select

        from mimir.models import Inbox
        from mimir.related import related_discussions

        with seeded_db() as s:
            alpha = s.execute(
                sa_select(Inbox).where(Inbox.name == "alpha")
            ).scalar_one()
            root, reply, tok_root, part_root, unrelated = self._setup_corpus(s, alpha)

            with caplog.at_level(logging.INFO, logger="mimir.related"):
                out = related_discussions(
                    s,
                    alpha,
                    root_id=root.id,
                    thread_article_ids={root.id, reply.id},
                    thread_authors={"Alice", "Bob"},
                )

            ids = [r.article_id for r in out]
            assert tok_root.id in ids
            assert part_root.id in ids
            assert unrelated.id not in ids
            assert root.id not in ids and reply.id not in ids
            # Token match outranks participant-only.
            assert ids.index(tok_root.id) < ids.index(part_root.id)
            tok_item = next(r for r in out if r.article_id == tok_root.id)
            part_item = next(r for r in out if r.article_id == part_root.id)
            assert "token" in tok_item.signals
            assert part_item.signals == ("participant",)
            # Instrumentation line carries the decision-rule fields.
            line = next(
                rec.getMessage()
                for rec in caplog.records
                if rec.getMessage().startswith("related-discussions:")
            )
            for field in (
                "inbox=alpha",
                "candidates=",
                "rendered=2",
                "strong=1",
                "weak=1",
                "top=",
                "elapsed_ms=",
            ):
                assert field in line, line

    def test_result_is_cached(self, seeded_db):
        from sqlalchemy import select as sa_select

        from mimir import cache
        from mimir.models import Inbox
        from mimir.related import related_discussions

        with seeded_db() as s:
            alpha = s.execute(
                sa_select(Inbox).where(Inbox.name == "alpha")
            ).scalar_one()
            root, reply, *_ = self._setup_corpus(s, alpha)
            related_discussions(
                s,
                alpha,
                root_id=root.id,
                thread_article_ids={root.id, reply.id},
                thread_authors={"Alice", "Bob"},
            )
            assert cache.get(f"related_discussions:alpha:{root.id}") is not None

    def test_replies_collapse_to_their_root(self, seeded_db):
        """A candidate that is a REPLY in a prior thread must surface
        as that thread's root, not as the reply row."""
        from sqlalchemy import select as sa_select

        from mimir.models import Inbox
        from mimir.related import related_discussions

        with seeded_db() as s:
            alpha = s.execute(
                sa_select(Inbox).where(Inbox.name == "alpha")
            ).scalar_one()
            root, reply, tok_root, *_ = self._setup_corpus(s, alpha)
            tok_reply = _seed_message(
                s,
                alpha,
                "tok-reply@x",
                "Re: journal replay hangs on dirty mount",
                "Dave",
                59,
                thread_parent="tok-root@x",
            )
            s.commit()
            out = related_discussions(
                s,
                alpha,
                root_id=root.id,
                thread_article_ids={root.id, reply.id},
                thread_authors={"Alice", "Bob"},
            )
            ids = [r.article_id for r in out]
            assert tok_root.id in ids
            assert tok_reply.id not in ids
