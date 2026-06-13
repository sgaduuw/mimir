"""Tests for bot-sender exclusion in the related-discussions panel
(#71 follow-up)."""


class TestIsBotSender:
    def test_matches_default_bot_substrings_case_insensitive(self):
        from mimir.related import is_bot_sender

        # syzbot, kernel test robot (lkp), tip-bot: the three defaults.
        assert is_bot_sender("syzbot <syzbot+deadbeef@syzkaller.appspotmail.com>")
        assert is_bot_sender("kernel test robot <lkp@intel.com>")
        assert is_bot_sender("tip-bot2 for Foo Bar <tip-bot2@linutronix.de>")
        # Case-insensitive on the address.
        assert is_bot_sender("SYZBOT <X@SYZKALLER.APPSPOTMAIL.COM>")

    def test_humans_are_not_bots(self):
        from mimir.related import is_bot_sender

        assert not is_bot_sender("Linus Torvalds <torvalds@linux-foundation.org>")
        assert not is_bot_sender("Stephen Rothwell <sfr@canb.auug.org.au>")

    def test_none_and_empty_are_not_bots(self):
        from mimir.related import is_bot_sender

        assert not is_bot_sender(None)
        assert not is_bot_sender("")

    def test_respects_settings_override(self, monkeypatch):
        from mimir.config import settings
        from mimir.related import is_bot_sender

        monkeypatch.setattr(
            settings, "related_discussions_bot_senders", ["robo@x.test"]
        )
        assert is_bot_sender("Some Robot <robo@x.test>")
        # Default bots no longer match once the list is overridden.
        assert not is_bot_sender("syzbot <x@syzkaller.appspotmail.com>")


class TestBotExclusionInScorer:
    def _alpha(self, s):
        from sqlalchemy import select as sa_select

        from mimir.models import Inbox

        return s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()

    def test_bot_rooted_candidate_is_excluded(self, seeded_db):
        """A thread whose root author is a bot must not surface as a
        related thread, even when it shares a strong token, and it
        must be counted in bot_filtered."""
        from mimir.related import related_discussions
        from tests.test_related import (
            _metric_line,
            _seed_message,
            capture_metric_lines,
        )

        with seeded_db() as s:
            alpha = self._alpha(s)
            root = _seed_message(
                s,
                alpha,
                "human-root@x",
                "bcachefs deadlock during journal replay",
                "Alice",
                2,
            )
            # Bot-authored thread sharing the 'journal' token.
            _seed_message(
                s,
                alpha,
                "bot-cand@x",
                "journal replay warning in fs",
                "syzbot <x@syzkaller.appspotmail.com>",
                30,
            )
            # Human thread sharing the same token (control: must appear).
            human = _seed_message(
                s,
                alpha,
                "human-cand@x",
                "journal replay hangs on dirty mount",
                "Carol",
                40,
            )
            s.commit()
            with capture_metric_lines() as buf:
                out = related_discussions(
                    s,
                    alpha,
                    root_id=root.id,
                    thread_article_ids={root.id},
                    thread_authors={"Alice"},
                )
            ids = [r.article_id for r in out]
            assert human.id in ids
            assert all(r.subject != "journal replay warning in fs" for r in out)
            line = _metric_line(buf)
            assert "bot_filtered=1" in line, line

    def test_bot_coparticipant_does_not_generate_candidates(self, seeded_db):
        """When a bot is among the thread's authors, it must not pull
        in threads related only by sharing that bot as a participant."""
        from mimir.related import related_discussions
        from tests.test_related import _seed_message

        with seeded_db() as s:
            alpha = self._alpha(s)
            root = _seed_message(
                s,
                alpha,
                "cop-root@x",
                "some unique human discussion",
                "Alice",
                2,
            )
            # A thread sharing ONLY the bot author, no token/subject overlap.
            _seed_message(
                s,
                alpha,
                "bot-only@x",
                "totally different subject about hardware",
                "syzbot <x@syzkaller.appspotmail.com>",
                20,
            )
            s.commit()
            out = related_discussions(
                s,
                alpha,
                root_id=root.id,
                thread_article_ids={root.id},
                # The bot is a co-author of the current thread.
                thread_authors={"Alice", "syzbot <x@syzkaller.appspotmail.com>"},
            )
            assert out == []
