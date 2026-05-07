"""Service-layer contract for `mimir.inboxes`.

Validators (pure functions, no DB) plus CRUD ops that gate the
admin CLI today and will gate the future admin web UI (#10). Cache
invalidation on rename / delete is also pinned here — that's the
crossover with `cache.delete_for_inbox`.
"""
import pytest

from mimir.inboxes import (
    InboxNotFound,
    InboxValidationError,
    validate_mirror_path,
    validate_name,
    validate_tracked_authors,
    validate_upstream_url,
)


# Pure-function validators — no DB needed.


@pytest.mark.parametrize("name", [
    "lkml",
    "linux-fsdevel",
    "abc",
    "a",
    "ab",
    "0",
    "0a",
    "a0",
    "a-b",
    "a" * 64,                 # at the cap
])
def test_validate_name_accepts(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", [
    "",
    " ",
    "-",
    "-foo",                   # leading hyphen
    "foo-",                   # trailing hyphen
    "Foo",                    # uppercase
    "FOO",
    "a:b",                    # colon — cache-key separator
    "a/b",                    # slash — URL separator
    "a b",                    # whitespace
    "a@b",                    # @
    "a.b",                    # period
    "a" * 65,                 # over the cap
])
def test_validate_name_rejects(name):
    with pytest.raises(InboxValidationError):
        validate_name(name)


def test_validate_name_strips_whitespace():
    assert validate_name("  lkml  ") == "lkml"


def test_validate_name_rejects_non_string():
    with pytest.raises(InboxValidationError):
        validate_name(None)


@pytest.mark.parametrize("url", [
    "https://example.com",
    "https://lore.kernel.org/lkml",
    "https://example.com/some/long/path",
])
def test_validate_upstream_url_accepts_https(url):
    assert validate_upstream_url(url) == url


@pytest.mark.parametrize("url", [
    "http://example.com",      # plain http
    "ftp://example.com",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://",                # missing host
    "https:///path",
    "",
])
def test_validate_upstream_url_rejects(url):
    with pytest.raises(InboxValidationError):
        validate_upstream_url(url)


def test_validate_upstream_url_strips_whitespace():
    assert validate_upstream_url(" https://x.com ") == "https://x.com"


def test_validate_mirror_path_rejects_empty():
    with pytest.raises(InboxValidationError):
        validate_mirror_path("")
    with pytest.raises(InboxValidationError):
        validate_mirror_path("   ")


def test_validate_mirror_path_strips_whitespace():
    assert validate_mirror_path("  /tmp/x  ") == "/tmp/x"


# CRUD via `seeded_db` fixture.


def test_list_inboxes_returns_seeded(seeded_db):
    from mimir.inboxes import list_inboxes
    names = sorted(ix.name for ix in list_inboxes())
    assert names == ["alpha", "beta"]


def test_get_inbox_existing(seeded_db):
    from mimir.inboxes import get_inbox
    ix = get_inbox("alpha")
    assert ix.name == "alpha"
    assert ix.upstream_url == "https://example.com/alpha"


def test_get_inbox_missing_raises(seeded_db):
    from mimir.inboxes import get_inbox
    with pytest.raises(InboxNotFound):
        get_inbox("nonexistent")


def test_create_inbox_inserts(seeded_db):
    from mimir.inboxes import create_inbox, list_inboxes
    create_inbox(
        "gamma",
        mirror_path="/tmp/gamma",
        upstream_url="https://example.com/gamma",
    )
    assert "gamma" in {ix.name for ix in list_inboxes()}


def test_create_inbox_duplicate_raises(seeded_db):
    from mimir.inboxes import create_inbox
    with pytest.raises(InboxValidationError, match="already exists"):
        create_inbox("alpha", mirror_path="/tmp/x", upstream_url="https://x.com")


def test_create_inbox_validates_name(seeded_db):
    from mimir.inboxes import create_inbox
    with pytest.raises(InboxValidationError):
        create_inbox("BadName", mirror_path="/tmp/x", upstream_url="https://x.com")


def test_create_inbox_validates_url(seeded_db):
    from mimir.inboxes import create_inbox
    with pytest.raises(InboxValidationError):
        create_inbox("foo", mirror_path="/tmp/foo", upstream_url="http://insecure.com")


def test_update_inbox_changes_fields(seeded_db):
    from mimir.inboxes import get_inbox, update_inbox
    update_inbox(
        "alpha",
        mirror_path="/tmp/alpha-new",
        upstream_url="https://example.com/alpha-v2",
    )
    ix = get_inbox("alpha")
    assert ix.mirror_path == "/tmp/alpha-new"
    assert ix.upstream_url == "https://example.com/alpha-v2"


def test_update_inbox_rename(seeded_db):
    from mimir.inboxes import get_inbox, update_inbox
    update_inbox("alpha", new_name="alpha-renamed")
    with pytest.raises(InboxNotFound):
        get_inbox("alpha")
    ix = get_inbox("alpha-renamed")
    assert ix.name == "alpha-renamed"


def test_update_inbox_rename_collision_raises(seeded_db):
    from mimir.inboxes import update_inbox
    with pytest.raises(InboxValidationError, match="already exists"):
        update_inbox("alpha", new_name="beta")


def test_update_inbox_unknown_raises(seeded_db):
    from mimir.inboxes import update_inbox
    with pytest.raises(InboxNotFound):
        update_inbox("nonexistent", mirror_path="/tmp/x")


def test_delete_inbox_cascades_article_lists(seeded_db):
    """Removing alpha should drop its article_lists rows but leave
    beta's untouched."""
    from sqlalchemy import select

    from mimir.inboxes import delete_inbox
    from mimir.models import ArticleList, Inbox

    delete_inbox("alpha", keep_orphan_articles=True)

    with seeded_db() as s:
        # alpha is gone
        assert s.execute(
            select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one_or_none() is None
        # alpha's article_lists rows are gone
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        all_rows = s.execute(select(ArticleList)).scalars().all()
        for row in all_rows:
            assert row.inbox_id == beta.id


def test_delete_inbox_removes_orphan_articles(seeded_db):
    """art1 and art4 are alpha-only — deleting alpha should drop
    them. art3 is cross-posted, art2 is beta-only — both survive."""
    from sqlalchemy import select

    from mimir.inboxes import delete_inbox
    from mimir.models import Article

    delete_inbox("alpha", keep_orphan_articles=False)

    with seeded_db() as s:
        message_ids = sorted(
            mid for mid, in s.execute(select(Article.message_id))
        )
    assert "art1@example.com" not in message_ids
    assert "art4@example.com" not in message_ids
    assert "art2@example.com" in message_ids
    assert "art3@example.com" in message_ids


def test_delete_inbox_keep_orphans_preserves_articles(seeded_db):
    from sqlalchemy import select

    from mimir.inboxes import delete_inbox
    from mimir.models import Article

    delete_inbox("alpha", keep_orphan_articles=True)

    with seeded_db() as s:
        ids = sorted(mid for mid, in s.execute(select(Article.message_id)))
    # All four articles still in DB; some are now unlinked.
    assert ids == ["art1@example.com", "art2@example.com",
                   "art3@example.com", "art4@example.com"]


def test_delete_inbox_unknown_raises(seeded_db):
    from mimir.inboxes import delete_inbox
    with pytest.raises(InboxNotFound):
        delete_inbox("nonexistent")


def test_delete_inbox_invalidates_cache(seeded_db):
    """Cache rows for the deleted inbox must not survive."""
    from mimir import cache
    from mimir.inboxes import delete_inbox

    cache.set("archive_stats:alpha", {"sentinel": True}, ttl=3600)
    cache.set("daily_volume:alpha:30", {"sentinel": True}, ttl=3600)
    cache.set("archive_stats:beta", {"sentinel": True}, ttl=3600)

    delete_inbox("alpha", keep_orphan_articles=True)

    keys = set(cache.keys())
    assert "archive_stats:alpha" not in keys
    assert "daily_volume:alpha:30" not in keys
    assert "archive_stats:beta" in keys


def test_update_inbox_rename_invalidates_cache(seeded_db):
    """Renaming alpha → alpha2 should drop the alpha-named cache
    rows; the new name's reads will repopulate fresh."""
    from mimir import cache
    from mimir.inboxes import update_inbox

    cache.set("archive_stats:alpha", {"sentinel": True}, ttl=3600)
    cache.set("daily_volume:alpha:30", {"sentinel": True}, ttl=3600)

    update_inbox("alpha", new_name="alpha2")

    keys = set(cache.keys())
    assert "archive_stats:alpha" not in keys
    assert "daily_volume:alpha:30" not in keys


def test_update_inbox_no_rename_keeps_cache(seeded_db):
    """A non-rename update (mirror_path / upstream_url change)
    doesn't touch the cache."""
    from mimir import cache
    from mimir.inboxes import update_inbox

    cache.set("archive_stats:alpha", {"sentinel": True}, ttl=3600)
    update_inbox("alpha", mirror_path="/tmp/alpha-v2")

    assert "archive_stats:alpha" in set(cache.keys())


# Tracked authors — validators, mutators, NULL/dict round-trip.


def test_validate_tracked_authors_accepts_dict():
    out = validate_tracked_authors({"Linus": "torvalds@", "Greg KH": "gregkh@"})
    assert out == {"Linus": "torvalds@", "Greg KH": "gregkh@"}


def test_validate_tracked_authors_strips_whitespace():
    out = validate_tracked_authors({"  Linus  ": "  torvalds@  "})
    assert out == {"Linus": "torvalds@"}


def test_validate_tracked_authors_normalizes_empty_to_none():
    assert validate_tracked_authors({}) is None
    assert validate_tracked_authors(None) is None


@pytest.mark.parametrize("authors", [
    "not a dict",
    ["Linus", "torvalds@"],
    {"": "torvalds@"},                 # empty label
    {"Linus": ""},                     # empty substring
    {"   ": "torvalds@"},              # whitespace-only label
    {"Linus": "   "},                  # whitespace-only substring
    {"Linus": 42},                     # non-string value
    {42: "torvalds@"},                 # non-string key
    {"L" * 65: "torvalds@"},           # label over cap
    {"Linus": "x" * 257},              # substring over cap
])
def test_validate_tracked_authors_rejects(authors):
    with pytest.raises(InboxValidationError):
        validate_tracked_authors(authors)


def test_set_tracked_authors_round_trip(seeded_db):
    from mimir.inboxes import get_inbox, set_tracked_authors
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    ix = get_inbox("alpha")
    assert ix.tracked_authors == {"Linus": "torvalds@", "Greg": "gregkh@"}


def test_set_tracked_authors_none_writes_null(seeded_db):
    from mimir.inboxes import get_inbox, set_tracked_authors
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    set_tracked_authors("alpha", None)
    ix = get_inbox("alpha")
    assert ix.tracked_authors is None


def test_set_tracked_authors_empty_dict_writes_null(seeded_db):
    """Empty dict and NULL collapse — both mean "no tracker tiles"."""
    from mimir.inboxes import get_inbox, set_tracked_authors
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    set_tracked_authors("alpha", {})
    ix = get_inbox("alpha")
    assert ix.tracked_authors is None


def test_set_tracked_authors_unknown_raises(seeded_db):
    from mimir.inboxes import set_tracked_authors
    with pytest.raises(InboxNotFound):
        set_tracked_authors("nonexistent", {"Linus": "torvalds@"})


def test_set_tracked_authors_validates(seeded_db):
    from mimir.inboxes import set_tracked_authors
    with pytest.raises(InboxValidationError):
        set_tracked_authors("alpha", {"": "torvalds@"})


def test_add_tracked_author_initializes_null_inbox(seeded_db):
    from mimir.inboxes import add_tracked_author, get_inbox
    add_tracked_author("alpha", "Linus", "torvalds@")
    assert get_inbox("alpha").tracked_authors == {"Linus": "torvalds@"}


def test_add_tracked_author_appends(seeded_db):
    from mimir.inboxes import add_tracked_author, get_inbox
    add_tracked_author("alpha", "Linus", "torvalds@")
    add_tracked_author("alpha", "Greg", "gregkh@")
    assert get_inbox("alpha").tracked_authors == {
        "Linus": "torvalds@", "Greg": "gregkh@",
    }


def test_add_tracked_author_replaces_label(seeded_db):
    from mimir.inboxes import add_tracked_author, get_inbox
    add_tracked_author("alpha", "Linus", "torvalds@")
    add_tracked_author("alpha", "Linus", "linus@kernel.org")
    assert get_inbox("alpha").tracked_authors == {"Linus": "linus@kernel.org"}


def test_remove_tracked_author_drops_label(seeded_db):
    from mimir.inboxes import (
        get_inbox,
        remove_tracked_author,
        set_tracked_authors,
    )
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    remove_tracked_author("alpha", "Greg")
    assert get_inbox("alpha").tracked_authors == {"Linus": "torvalds@"}


def test_remove_tracked_author_last_entry_writes_null(seeded_db):
    from mimir.inboxes import (
        get_inbox,
        remove_tracked_author,
        set_tracked_authors,
    )
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    remove_tracked_author("alpha", "Linus")
    assert get_inbox("alpha").tracked_authors is None


def test_remove_tracked_author_missing_label_raises(seeded_db):
    from mimir.inboxes import remove_tracked_author, set_tracked_authors
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    with pytest.raises(InboxValidationError, match="no tracker labelled"):
        remove_tracked_author("alpha", "Greg")


def test_remove_tracked_author_on_null_raises(seeded_db):
    """Removing from an inbox that has no trackers configured at all."""
    from mimir.inboxes import remove_tracked_author
    with pytest.raises(InboxValidationError, match="no tracker labelled"):
        remove_tracked_author("alpha", "Linus")


def test_clear_tracked_authors_writes_null(seeded_db):
    from mimir.inboxes import (
        clear_tracked_authors,
        get_inbox,
        set_tracked_authors,
    )
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    clear_tracked_authors("alpha")
    assert get_inbox("alpha").tracked_authors is None


def test_clear_tracked_authors_unknown_raises(seeded_db):
    from mimir.inboxes import clear_tracked_authors
    with pytest.raises(InboxNotFound):
        clear_tracked_authors("nonexistent")
