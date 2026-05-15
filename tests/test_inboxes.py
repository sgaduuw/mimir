"""Service-layer contract for `mimir.inboxes`.

Validators (pure functions, no DB) plus CRUD ops that gate the
admin CLI today and will gate the future admin web UI (#10). Cache
invalidation on rename / delete is also pinned here; that's the
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


# Pure-function validators, no DB needed.


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
    "a:b",                    # colon, cache-key separator
    "a/b",                    # slash, URL separator
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
    """art1 and art4 are alpha-only, deleting alpha should drop
    them. art3 is cross-posted, art2 is beta-only, both survive."""
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


# Tracked authors, validators, mutators, NULL/dict round-trip.


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
    """Empty dict and NULL collapse, both mean "no tracker tiles"."""
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


# --------------------------------------------------------------------------
# Service-layer surfaces previously without direct tests:
# - bootstrap_inboxes (env reconciliation, runs on every startup)
# - InboxRemovalReport counts on delete_inbox
# - remove_inbox_data side effect of delete_inbox
# --------------------------------------------------------------------------


def test_bootstrap_inboxes_inserts_env_rows_then_no_ops_on_rerun(
    seeded_db, monkeypatch,
):
    """Bootstrap inserts any env-declared inbox missing from the DB,
    and a second call is a no-op (idempotent insert). The contract
    is `INSERT ... ON CONFLICT DO NOTHING`; without idempotence the
    web container's startup would race the scheduler sidecar's
    parallel bootstrap and trip a UNIQUE violation."""
    from sqlalchemy import select
    from mimir.config import InboxConfig, settings
    from mimir.inboxes import bootstrap_inboxes
    from mimir.models import Inbox

    monkeypatch.setattr(settings, "inboxes", {
        "fresh-inbox": InboxConfig(
            mirror_path="/tmp/fresh-inbox/git",
            upstream_url="https://example.com/fresh-inbox",
        ),
    })

    # First call: row inserted.
    out_1 = bootstrap_inboxes()
    assert "fresh-inbox" in out_1
    with seeded_db() as s:
        row = s.execute(
            select(Inbox).where(Inbox.name == "fresh-inbox")
        ).scalar_one()
        assert row.mirror_path == "/tmp/fresh-inbox/git"

    # Second call: no-op, same DB state.
    out_2 = bootstrap_inboxes()
    assert set(out_2.keys()) == set(out_1.keys())
    with seeded_db() as s:
        row_after = s.execute(
            select(Inbox).where(Inbox.name == "fresh-inbox")
        ).scalar_one()
        assert row_after.id == row.id
        assert row_after.mirror_path == "/tmp/fresh-inbox/git"


def test_bootstrap_inboxes_preserves_admin_managed_rows(
    seeded_db, monkeypatch,
):
    """A row added via the admin CLI (not present in `settings.inboxes`)
    must survive a bootstrap pass intact. Without this contract, every
    container restart would orphan operator changes."""
    from sqlalchemy import select
    from mimir.config import settings
    from mimir.inboxes import bootstrap_inboxes
    from mimir.models import Inbox

    # Empty env: nothing to insert. alpha + beta are admin-managed
    # by the seed fixture's standards (they came from the test
    # fixture, not the env).
    monkeypatch.setattr(settings, "inboxes", {})

    bootstrap_inboxes()

    with seeded_db() as s:
        names = sorted(
            n for n, in s.execute(select(Inbox.name))
        )
    # The seeded admin-managed rows still exist; bootstrap didn't
    # delete or rename them.
    assert "alpha" in names
    assert "beta" in names


def test_bootstrap_inboxes_does_not_overwrite_admin_edits(
    seeded_db, monkeypatch,
):
    """If env declares an inbox whose name already exists in the DB
    (e.g. the operator's `admin inbox update` edited the mirror_path),
    bootstrap must NOT overwrite the operator's value. The
    `on_conflict_do_nothing` is the load-bearing piece -- a refactor
    to `on_conflict_do_update` would silently undo admin edits on
    the next container restart."""
    from sqlalchemy import select
    from mimir.config import InboxConfig, settings
    from mimir.inboxes import bootstrap_inboxes
    from mimir.models import Inbox

    # Env declares `alpha` with a different mirror_path than the
    # seeded fixture's `/tmp/alpha`. Bootstrap must leave the
    # existing row alone.
    monkeypatch.setattr(settings, "inboxes", {
        "alpha": InboxConfig(
            mirror_path="/different/from/seeded",
            upstream_url="https://different.example/alpha",
        ),
    })

    bootstrap_inboxes()

    with seeded_db() as s:
        alpha = s.execute(
            select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one()
    assert alpha.mirror_path == "/tmp/alpha", (
        "bootstrap clobbered an admin-managed mirror_path; the conflict "
        "policy must stay DO NOTHING, not DO UPDATE"
    )
    assert alpha.upstream_url == "https://example.com/alpha"


def test_delete_inbox_report_counts_pin_the_cascade(seeded_db):
    """`InboxRemovalReport`'s counters are the operator-facing summary
    of what `delete_inbox` did. The seeded fixture gives alpha three
    ArticleList rows (art1, art3, art4) and no IngestState rows;
    art1 + art4 are alpha-only so they orphan on delete. Pin all
    three counts so a refactor that off-by-ones the tally is
    surfaced."""
    from mimir.inboxes import delete_inbox

    report = delete_inbox("alpha", keep_orphan_articles=False)
    assert report.name == "alpha"
    # 3 ArticleList rows on alpha: art1, art3, art4.
    assert report.article_lists_deleted == 3
    # No IngestState rows seeded for alpha.
    assert report.ingest_state_deleted == 0
    # art1 + art4 are alpha-only; art3 cross-posts to beta -> survives.
    assert report.orphan_articles_deleted == 2
    # No mirror-data removal requested.
    assert report.mirror_path_deleted is None


def test_delete_inbox_keep_orphans_zeroes_orphan_count(seeded_db):
    """`keep_orphan_articles=True` skips the orphan-delete pass; the
    counter must reflect that (0, not the count that *would* have
    been deleted)."""
    from mimir.inboxes import delete_inbox
    report = delete_inbox("alpha", keep_orphan_articles=True)
    assert report.orphan_articles_deleted == 0


def test_delete_inbox_remove_inbox_data_rms_mirror_dir(
    seeded_db, tmp_path,
):
    """`remove_inbox_data=True` rm -rf's the on-disk mirror. The
    mirror_path follows `Inboxes/<name>/git`; `delete_inbox` strips
    the trailing `git` segment so the per-inbox wrapper directory
    goes too. The report carries the path actually deleted."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.inboxes import delete_inbox
    from mimir.models import Inbox

    # Build a realistic mirror layout: tmp/alpha/git/0.git/.
    wrapper = tmp_path / "alpha"
    git_dir = wrapper / "git"
    epoch_dir = git_dir / "0.git"
    epoch_dir.mkdir(parents=True)
    (epoch_dir / "HEAD").write_text("ref: refs/heads/master\n")

    # Point the seeded alpha at this layout so delete_inbox can find it.
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(git_dir)
        s.commit()

    report = delete_inbox("alpha", remove_inbox_data=True)

    assert report.mirror_path_deleted == str(wrapper), (
        f"expected wrapper dir to be the removal target, got "
        f"{report.mirror_path_deleted!r}"
    )
    # The wrapper and its contents are gone.
    assert not wrapper.exists()
    # tmp_path itself (the parent we don't own) is untouched.
    assert tmp_path.exists()


def test_delete_inbox_remove_inbox_data_handles_missing_mirror(
    seeded_db, tmp_path,
):
    """`remove_inbox_data=True` against a mirror_path that doesn't
    exist on disk must NOT raise; the row deletion still succeeds
    and the report records nothing was actually rm'd."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.inboxes import delete_inbox
    from mimir.models import Inbox

    nonexistent = tmp_path / "no-such-mirror" / "git"
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(nonexistent)
        s.commit()

    report = delete_inbox("alpha", remove_inbox_data=True)
    assert report.mirror_path_deleted is None
    # And the DB row is gone.
    with SessionLocal() as s:
        assert s.execute(
            select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one_or_none() is None


# --------------------------------------------------------------------------
# _INBOX_NAMES module cache: republish after every CRUD op.
#
# `inbox_names()` is what nav rendering reads, cheap (no DB hit).
# Every CRUD function in `mimir.inboxes` calls `_publish_names()` to
# keep it fresh. Existing tests all go through DB-backed reads
# (`list_inboxes`, `get_inbox`), which means a regression where
# `_publish_names()` stopped firing would silently break nav while
# every functional test stayed green. Pin the republish via
# `inbox_names()` directly.
# --------------------------------------------------------------------------


def test_inbox_names_cache_refreshes_after_create_update_delete(seeded_db):
    """Each CRUD op must republish the nav-name cache. Exercise the
    three ops back-to-back so a regression in any one of the
    `_publish_names` call sites surfaces."""
    from mimir.inboxes import (
        create_inbox, delete_inbox, inbox_names, update_inbox,
    )

    baseline = set(inbox_names())
    assert {"alpha", "beta"}.issubset(baseline), (
        f"seed should publish alpha+beta into the name cache; got {baseline}"
    )

    # CREATE, cache picks up the new name.
    create_inbox(
        name="xtest-cache-refresh",
        upstream_url="https://example.com/xtest",
        mirror_path="/tmp/xtest-cache-refresh",
    )
    assert "xtest-cache-refresh" in inbox_names()

    # UPDATE (rename), old name out, new name in.
    update_inbox("xtest-cache-refresh", new_name="xtest-cache-renamed")
    names = inbox_names()
    assert "xtest-cache-refresh" not in names
    assert "xtest-cache-renamed" in names

    # DELETE, name leaves the cache.
    delete_inbox("xtest-cache-renamed")
    assert "xtest-cache-renamed" not in inbox_names()
