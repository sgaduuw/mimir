# Changelog

All notable user-facing changes to mimir.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe behaviour, schema, config, and CLI/route shape changes —
not internal refactors. Categories: **Added**, **Changed**, **Deprecated**,
**Removed**, **Fixed**, **Security**.

## [Unreleased]

### Added

- Persist parse failures: every commit whose `m` blob can't be parsed
  during ingest lands in a new `parse_failures` table keyed by
  `(inbox, epoch, commit_sha)` with `error_class`, `error_message`,
  `first_seen`, `last_attempt`, `attempts`. Cleared automatically when
  a re-walk parses the commit cleanly.
- `flask --app mimir admin failures list` — enumerate persisted failures,
  filter by `--inbox` / `--epoch` / `--error-class`.
- `flask --app mimir admin failures replay <inbox>` — re-fetch each
  failure's blob, re-run the parser, insert the article (or cross-post
  link) on success, bump `attempts` on continued failure. Use after a
  parser fix.
- CI publishes the Docker image to `ghcr.io/sgaduuw/mimir` on every
  push to `main` (`:main`, `:sha-<short>`) and on `v*` tags
  (`:<version>`, `:<major>.<minor>`, `:latest`). PRs still build
  for verification only.

## [0.1.0]

First tracked release. Summary of what shipped in the public-inbox
indexer line (post-rewrite from the early NNTP/mongo prototype).

### Added

- public-inbox v2 ingest: dulwich walker over per-epoch repos, parallel
  parse via `ProcessPoolExecutor`, per-(inbox, epoch) `IngestState`
  checkpoints, five outcome buckets (`new` / `linked` / `dup_batch` /
  `dup_db` / `failed`).
- Multi-inbox support with cross-post dedup: one `articles` row, one
  `article_lists` row per inbox.
- Read path: `mimir.store.read_message` does SQL lookup + dulwich blob
  fetch + `parse_message`; bodies / full headers / attachment bytes are
  never stored in SQLite.
- Web UI: per-inbox dashboard (active threads, trackers, pulls, releases,
  this-day-in-history, sparkline, archive stats); daily / monthly /
  yearly archive views; subject + author search; per-author chronological
  view; Message-ID lookup at `/m/<id>` and `/<inbox>/m/<id>`; per-message
  view with thread tree and JWZ subject grouping; attachment download +
  Pygments-highlighted preview; HTMX-driven "load more" recents.
- Atom feeds (per-inbox, per-author).
- Admin CLI for inbox CRUD with cache invalidation
  (`flask --app mimir admin inbox …`).
- Sync command (`flask --app mimir update`): manifest discovery,
  `git clone --mirror` + `git fetch --prune`, then ingest.
- Operations CLI: `warm-cache`, `analyze`, `vacuum`, `reindex`, `show`.
- Deployment artifacts: multi-stage Dockerfile, `compose.yaml`,
  systemd units + timers, Caddy and nginx reverse-proxy examples.
- Observability: `/healthz` + `/readyz` probes, structured per-request
  logging with request IDs.
- Standards files: `robots.txt`, `sitemap.xml`, `security.txt`
  (RFC 9116, gated on `SECURITY_CONTACT`).
- DB-backed cache (JSON-encoded with a small dataclass registry) +
  `cache.get_or_compute(session, key, ttl, fn)`; `CACHE_NAMESPACE_VERSION`
  cache-buster.
- GitHub Actions CI.
- Tests: parser contracts, cache encoder/decoder, route smoke,
  rendering pipeline, inbox service layer, threading CTEs, dashboard
  helpers, ingest outcome bucketing, store / web helpers.

### Security

- Response headers: CSP, HSTS, X-Frame-Options, Referrer-Policy,
  X-Content-Type-Options.
- Email-address redaction outside an allowlist (`<hidden>`); referenced
  Message-IDs render as neutral `[ref]` placeholders.
- 50 MB hard cap on `parse_message` input.
- `git clone` argv hardened against manifest-driven injection.
- Pinned CDN assets with SRI hashes.

[Unreleased]: https://github.com/sgaduuw/mimir/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sgaduuw/mimir/releases/tag/v0.1.0
