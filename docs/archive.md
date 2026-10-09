# Managing the archive

[README](../README.md) · [Operations](operations.md) · [Deployment](../deploy/README.md)

Run these commands from your source checkout with the broker running, as in
the [local setup](../README.md#try-it-locally). For Compose, use
`docker compose exec mimir-tasks mimir …` in place of `uv run mimir …`.
The client and broker must see the same database, socket, and mirror paths.

## Add or update a list

LKML and linux-fsdevel are configured by default. List names are lowercase
letters, digits and hyphens, at most 64 characters, with no leading or trailing hyphen.

```sh
uv run mimir admin inbox list
uv run mimir admin inbox show lkml
uv run mimir admin inbox add linux-arm-kernel
uv run mimir update --inbox linux-arm-kernel
```

`add` creates the configuration. By default, it uses
`Inboxes/<name>/git` and `https://lore.kernel.org/<name>`.
Use `--mirror-path PATH` and `--upstream-url URL` for another location;
upstream URLs must use HTTPS.

`update` discovers epochs through the upstream `manifest.js.gz`, clones
missing repositories, fetches existing ones, then ingests new messages.
Omit `--inbox` to update every configured list.

| Option | Effect |
| --- | --- |
| `--skip-clone` | Fetch existing epochs without discovering new ones |
| `--skip-fetch` | Discover and clone missing epochs without fetching existing ones |
| `--skip-ingest` | Download changes without indexing them |
| `-v` / `-vv` | Show progress / per-message detail |

Mirrors remain the source of truth at read time. Keep them on disk after ingest.
Archive size and clone time depend on the list; check upstream before downloading
its entire history. Mimir assumes append-only public-inbox repositories.

## Import a smaller sample

Clone one epoch and limit indexing before fetching an entire list:

```sh
mkdir -p Inboxes/linux-fsdevel/git
git clone --mirror -- https://lore.kernel.org/linux-fsdevel/git/0.git Inboxes/linux-fsdevel/git/0.git
uv run mimir ingest --inbox linux-fsdevel --limit 500 --workers 1
```

The clone still downloads the whole epoch. `--limit` bounds indexing, not
network transfer or mirror size. Do not run the periodic updater for a bounded
trial: a later `update` will discover the remaining epochs and ingest more data.

## Ingest existing mirrors

```sh
uv run mimir ingest
uv run mimir ingest --inbox lkml
uv run mimir ingest --inbox lkml --limit 500 --workers 1
uv run mimir show 'message-id@example.com'
uv run mimir show 'message-id@example.com' --inbox lkml --body-chars -1
```

Ingest parses messages in worker processes and sends database writes through
the broker. `--workers 1` is useful for debugging. Checkpoints are per inbox and
epoch; repeating an ingest resumes from the last recorded commit.

Each epoch summary reports:

| Counter | Meaning |
| --- | --- |
| `new` | New message inserted |
| `linked` | Existing message linked to another inbox |
| `dup_batch` | Message-ID already seen in the current batch |
| `dup_db` | Message already indexed and linked to this inbox |
| `failed` | Parsing failed; recorded for retry |

Existing messages are not overwritten. A cross-post shares one article row,
but retains a separate mirror pointer for each inbox. Parse failures are recorded
even though the epoch checkpoint advances.

## Retry failures or reindex an epoch

```sh
uv run mimir admin failures list
uv run mimir admin failures list --inbox lkml --error-class ValueError
uv run mimir admin failures replay lkml --epoch 0.git --limit 100
uv run mimir reindex lkml 0.git
```

Replay reads the failed blobs again. Success removes the failure record;
another failure updates its attempt count. A missing commit or blob is skipped.
Reindex rewinds the epoch checkpoint and walks it again, skipping existing rows.

`uv run mimir reindex lkml 0.git --from-scratch` also deletes that inbox's
links to the epoch before walking it. Article rows shared by other inboxes remain.
It rebuilds the inbox's thread roots even if the walk fails, because removing
links can break reply chains across epochs. The broker needs access to the mirror;
`-v` reports where to find its logs.

## Change or remove an inbox

```sh
uv run mimir admin inbox update lkml --mirror-path /srv/mirrors/lkml/git
uv run mimir admin inbox update old-name --rename new-name
uv run mimir admin inbox update netdev --list-address netdev@vger.kernel.org
uv run mimir admin inbox update netdev --list-address ''
uv run mimir admin inbox trackers set lkml 'Linus=torvalds@'
uv run mimir admin inbox trackers add lkml Greg 'gregkh@'
uv run mimir admin inbox trackers remove lkml Greg
uv run mimir admin inbox trackers clear lkml
uv run mimir admin inbox remove old-name
```

`--list-address` is stored lowercase and must be a bare address. It is refused
when another inbox holds it. Canonical resolution only matches addresses on a
known list host, so for any other host add it to `LIST_HOST_SUFFIX_OVERRIDES`
(JSON array) or the command warns that the value never matches. `''` clears it
and hands the inbox back to auto-detection, which can promote a cross-post
address again. It does not recompute canonicals; run
`uv run mimir admin canonicals backfill --reprocess` afterwards.

Inbox edits live in SQLite and survive restarts. The `INBOXES` environment
setting is a bootstrap source, not an override for existing rows.
Renames and removals invalidate caches referring to the old name.

Removal deletes inbox links and checkpoints, and normally deletes articles
left without any inbox. Use `--keep-orphan-articles` to retain those articles.
Mirrors stay on disk unless you pass **`--remove-inbox-data`**, which permanently
deletes the mirror directory. The command asks for confirmation; `--yes` skips it.

## Track kernel trees and maintainers

```sh
uv run mimir update-mainline
uv run mimir update-mainline --skip-fetch
uv run mimir update-mainline --force
uv run mimir update-mainline --skip-commits
uv run mimir update-mainline --rewalk
```

This refreshes MAINTAINERS-derived subsystem ownership and walks tracked trees
for `Link:` and `Message-ID:` trailers connecting commits to archived patches. An unchanged
MAINTAINERS blob skips its reload; `--force` reloads it after a parser fix or
an out-of-band table reset. `--skip-commits` skips the commit walk.
`--rewalk` walks every tree's full history again, ignoring the cursor and the
walk interval; run it once after a change to which trailers are recognised.

The default tree set includes Linus, linux-next, and subsystem trees. To replace
it with your own set, configure `TREES__<name>__*` in the broker environment:

```sh
export TREES__example__URL=https://example.com/linux.git
export TREES__example__PATH=Mainline/example.git
export TREES__example__WALK_EVERY_SECONDS=3600
```

Any explicit tree set replaces the defaults; it does not merge with them.
The legacy `MAINLINE_TREE_URL` and `MAINLINE_TREE_PATH` settings seed only the
`linus` entry. See [config.py](../mimir/config.py) for the current settings.

## Backfill derived data

Newly ingested messages get derived metadata automatically. After adding an
extractor or correcting old data, use the corresponding backfill:

```sh
uv run mimir backfill-article-files --limit 1000
uv run mimir backfill-article-trailers --limit 1000
uv run mimir backfill-patch-series --limit 1000
uv run mimir backfill-thread-roots --verify
```

The first three accept `--reprocess` to revisit already processed articles.
File and trailer extraction read message bodies from the mirrors; patch-series
extraction uses indexed headers. Use `--help` for per-command options.

Thread roots are maintained by ingest, reindex, and failure replay. The broker
also fills missing roots at startup, so a manual backfill is normally unnecessary.
Interrupted fills resume on restart. Check for `backfill complete` with
`remaining=0`; `backfill incomplete` means work remains. `--verify` compares a
sample with an independent recursive query.
