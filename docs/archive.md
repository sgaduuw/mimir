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
epoch; repeating an ingest resumes from the last recorded commit. Pinned inboxes
(`PINNED_INBOXES`) go first, then the rest by name.

After the first ingest of an empty database (not a routine tick), run
`uv run mimir admin canonicals backfill --reprocess --restart` (add `--yes` if
it refuses to discard a run with progress). Cross-posts are linked before the
topical inboxes have promoted their own list address, so without it they pin to
the pinned inbox (lkml) instead of their topical list.

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
`uv run mimir admin canonicals backfill --reprocess --restart` afterwards (add
`--yes` if it refuses to discard a run with progress), so an unfinished earlier
run does not skip the articles above its cursor.

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
`--rewalk` walks every tree again from the start, ignoring the cursor and the
walk interval (every tree other than Linus's still skips commits already in
Linus's tree); run it once after an upgrade whose release notes ask for it,
such as a change to which trailers are recognised or to how rows are keyed.

The default tree set includes Linus, linux-next, and subsystem trees. To replace
it with your own set, configure `TREES__<name>__*` in the broker environment:

```sh
export TREES__example__URL=https://example.com/linux.git
export TREES__example__PATH=Mainline/example.git
export TREES__example__WALK_EVERY_SECONDS=3600
```

Any explicit tree set replaces the defaults; it does not merge with them.
Changing a tree's `URL` takes effect at its next walk: an existing mirror is
pointed at the new URL before it fetches, with a warning in the broker log.
See [config.py](../mimir/config.py) for the current settings.

## Backfill derived data

Newly ingested messages get derived metadata automatically. After adding an
extractor or correcting old data, use the corresponding backfill:

```sh
uv run mimir backfill-article-files --limit 1000
uv run mimir backfill-article-trailers --limit 1000
uv run mimir backfill-patch-series --limit 1000
uv run mimir backfill-series-chains --limit 1000
uv run mimir backfill-thread-roots --verify
```

The first three accept `--reprocess` to revisit already processed articles.
File and trailer extraction read message bodies from the mirrors; patch-series
extraction uses indexed headers. `backfill-patch-series` also rewrites a
stored subject that still carries header folding (a line break), in every
mode, and reports how many as `subjects_unfolded`. It also clears the series
of a reply that repeats its parent's subject without "Re:": a cover answering
that cover (`reply_cover`, #820) and a patch answering that patch with no diff
of its own (`reply_patch`, #837). A plain run skips rows that already have a
key, so only `--reprocess` clears a reply stored with one. The patch rule
reads `article_files`, so `backfill-article-files` must be complete first.
Use `--help` for per-command options.

`backfill-series-chains` splits a cover title that one sender reused for
unrelated postings into separate series. It also gives a posting whose cover
version is a typo its patches' version, on the cover and its patches (#870),
reported as `versions_planned`. Ingest and `backfill-patch-series` chain the
titles they touch, so it is needed once, for history ingested before 3.25.0.

These four, and `admin canonicals backfill`, resume an interrupted run. The
broker stores each run's position, so after a deploy, a dropped session or a
killed command, run the same command again and it continues from the last
committed batch (if it says the run is in progress instead, see below). It
prints `resuming run started <time> UTC at id N` when it does.
A run belongs to one command with one set of `--reprocess` and `--inbox`
values; a different `--inbox` starts its own run. `--limit` caps one session
only: the run stays open, and the next invocation continues it. A run that
reaches the oldest article is finished, and the next invocation starts a new
one at the newest article. Resuming is almost always what you want, so run
the same command again without `--restart`; `--restart` throws the run's
progress away.

Pass `--restart` to discard an unfinished run and start again at the newest
article. It prints `discarding run N started <time> UTC (E examined)` first,
with `at id C` after the time once the run has a position. If the run has
examined any articles, `--restart` refuses instead, prints that run and the
command that resumes it, and exits non-zero; add
`--yes` to discard it anyway. A run with no progress is discarded without
asking, and nothing extra is printed when there is no unfinished run. The
summary counts this session; the line after it gives the run's start time, its
total examined count, and whether the run is `finished` or still `open`. After
changing an inbox's `--list-address`, use `--restart`, with `--yes` if it
refuses: a resumed run only revisits articles below its cursor.

Only one session drives a run at a time. A command that finds the run's last
committed batch less than 300 seconds old stops with `run N is in progress
(last chunk <time> UTC, at id C); another session is driving it`, and changes
nothing; that holds for `--restart` and `--restart --yes` as well. Either the
other session is still running, or it was killed within the last few minutes:
the message says how many seconds to wait. To stop a run cleanly, run the
command with `podman exec -it`, so Ctrl-C reaches it, or kill the process
inside the container. Ctrl-C on `podman exec` without `-t` ends the client and
leaves the command running. Stopping loses at most the batch in flight, and
the next invocation resumes.

A session that ends on its own `--limit` releases the run, so the next
invocation continues it at once. A killed session cannot say it is gone, so
after Ctrl-C (even with `-it`) an immediate rerun is refused for up to 5
minutes: the broker only knows the run went quiet. Run it again after the wait
the refusal names.

Thread roots are maintained by ingest, reindex, and failure replay. The broker
also fills missing roots at startup, so a manual backfill is normally unnecessary.
Interrupted fills resume on restart. Check for `backfill complete` with
`remaining=0`; `backfill incomplete` means work remains. `--verify` compares a
sample with an independent recursive query.
