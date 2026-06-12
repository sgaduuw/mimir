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
