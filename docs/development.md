# Development

[README](../README.md) · [Web interface](web.md) · [Operations](operations.md)

## Local environment

Follow the [broker-first local setup](../README.md#try-it-locally).
Use Python 3.14 and `uv sync --locked`; the checked-in `.python-version` and
`uv.lock` select the interpreter and dependency versions.

Keep `MIMIR_IS_BROKER=true` scoped to the broker process. Other processes open
SQLite with `query_only=1` and send writes through its socket. The broker owns
migrations and inbox bootstrap, so a separate `alembic upgrade head` is not part
of the normal startup sequence.

For sample data, [clone one epoch and limit ingestion](archive.md#import-a-smaller-sample).
Run commands from the repository root so relative mirror and migration paths
resolve consistently. The source web server is for local development; use
[deployment instructions](../deploy/README.md) for a public site.

## Run checks

```sh
uv run ruff check mimir/ tests/
uv run ruff format --check mimir/ tests/
uv run pytest
```

Run each command directly and check its exit status. Lint and formatting are
separate gates, and both include tests. CI also tests the free-threaded Python
build and verifies that the container image builds.

The explicit Ruff rules live in [pyproject.toml](../pyproject.toml). Format only
touched files while working to keep unrelated changes out of a diff. For a
regression, demonstrate that the test fails before the fix.

The optional browser check uses Chromium and real CDN assets against a temporary
local database and Git mirror. It covers HTMX swaps, keyboard navigation, history,
load-more controls, failed requests, and links with JavaScript disabled:

```sh
uv run --frozen --with playwright playwright install chromium
uv run --frozen --with playwright pytest -q tests/test_routes/test_browser_navigation.py
```

## Data model

Git mirrors hold the original RFC 5322 messages. SQLite stores message identity,
selected display fields, threading hints, derived patch metadata, and pointers
to `(epoch, commit_sha)`. Bodies and attachment bytes are not copied into the DB.

| Model | Purpose |
| --- | --- |
| `Inbox` | List name, upstream, mirror location, trackers |
| `Article` | Deduplicated message identity and indexed metadata |
| `ArticleList` | Per-inbox membership and mirror pointer; materialized thread root |
| `IngestState` | Last processed commit per inbox and epoch |
| `ParseFailure` | Failed blob and retry history |
| `CacheEntry` | Serialized cached results with expiry |

See [models.py](../mimir/models.py) for the full schema and
[alembic/versions](../alembic/versions) for migrations. A null thread-root pointer
means uncomputed, not a root; a computed root points at itself.

Use `mimir.store.read_message` for a message or `read_messages` for a batch.
They locate and parse the Git blobs; the bulk path shares repository opens within
an epoch and omits unreadable messages rather than losing the whole batch.
All MIME parsing goes through `mimir.parser.parse_message`.

## Code map

| Location | Responsibility |
| --- | --- |
| [mimir/config.py](../mimir/config.py) | Environment-backed settings |
| [mimir/models.py](../mimir/models.py), [extensions.py](../mimir/extensions.py) | ORM models, connections, SQLite pragmas |
| [mimir/parser.py](../mimir/parser.py), [flowed.py](../mimir/flowed.py) | MIME extraction and format=flowed decoding |
| [mimir/store.py](../mimir/store.py), [sync.py](../mimir/sync.py) | Blob reads and mirror synchronization |
| [mimir/ingest](../mimir/ingest) | Incremental import, replay, and reindex |
| [mimir/broker](../mimir/broker) | Sole writer, RPC handlers, queues, and workers |
| [mimir/cli](../mimir/cli) | Click commands grouped by task |
| [mimir/rendering](../mimir/rendering) | Text, quotes, diffs, links, and highlighting |
| [mimir/web](../mimir/web), [templates](../mimir/templates) | Routes, hooks, filters, and Jinja templates |
| [mimir/seo](../mimir/seo) | Sitemaps, feeds, and structured metadata |
| [mimir/thread_roots.py](../mimir/thread_roots.py) | Materialized roots and verification |
| [mimir/subsystems_dashboard](../mimir/subsystems_dashboard) | Subsystem activity, reviewers, and triage |
| [tests](../tests) | Parser, renderer, routes, ingest, and broker regression checks |

## Contribution workflow

Branch from `develop`; `main` holds released code. Use `fix/`, `feature/`,
`refactor/`, `docs/`, or `chore/` branches and open PRs against `develop`.
Keep runtime changes covered by tests and add user-visible changes under
`Unreleased` in [CHANGELOG.md](../CHANGELOG.md).

Version changes belong on release branches. The version comes from
`pyproject.toml`, with the lockfile updated alongside it. See
[AGENTS.md](../AGENTS.md) for repository conventions and
[the workflows](../.github/workflows) for CI and release automation.
