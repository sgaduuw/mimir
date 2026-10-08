# AGENTS.md

Conventions for AI coding agents working on mimir. Tracked in the
repo deliberately: more than one agent and more than one tool work
on this codebase, and the conventions below are the ones whose
absence has actually caused problems. Read this before your first
change.

Operator and deployment specifics (host access, container names,
production paths, the post-deploy smoke) are **not** in the repo.
If a task needs them, say so and ask; do not guess at them.

This file says *what to do*. Its companion
[`CONTEXT.md`](CONTEXT.md), beside it at the repo root, says *why*,
at length: the
architecture, the decisions that look odd until you know what they
cost, and the defect classes this codebase keeps producing. It is
tracked too, and it is worth reading before a non-trivial change.
Where a rule here has a one-line justification, the full reasoning
is usually there.

## What mimir is, in one paragraph

A Flask app that indexes public-inbox v2 git mirrors of kernel
mailing lists. **The git mirror is the source of truth**; SQLite is
a thin index (~500 B/row) recording `(epoch, commit_sha)` pointers
plus a few display fields. The read path is SQL lookup, then a
dulwich blob fetch, then `parse_message`. Message bodies, full
headers and attachment bytes are **never** stored in SQLite. If a
feature needs the message bytes, route through
`mimir.store.read_message`.

## Tooling

uv for Python, alembic for migrations, ruff for lint, pytest for
tests.

```sh
uv sync
uv run pytest
uv run ruff check mimir/ tests/
uv run ruff format --check mimir/ tests/
uv run alembic upgrade head
```

### Judge a gate by its exit code, bare

Run `ruff check` and `ruff format --check` as separate commands with
**no pipe**, and read `$?`. Two independent gates; lint-clean is not
format-clean, and CI runs them as separate steps.

A pipe replaces the tool's exit status with the pipe's, so
`ruff check … | tail -1` both hides the `Found N errors` line and
lets an `&&` chain march on. That exact combination had a branch
described as "lint clean" through six exchanges while the gate
exited 1 with 114 errors. `tests/test_lint_gate.py` mirrors the
check in the suite because CI only reports after a push.

A scoped run is not the gate either: `--select SOMERULE` passing, or
one file passing, is not `ruff check mimir/ tests/` passing.

### The rule set is explicit, not ruff's defaults

`[tool.ruff.lint] select` in `pyproject.toml` pins the enabled
rules, because ruff's defaults moved from 59 rules to 413 between
0.15.21 and 0.16.x and a dependabot bump silently redefined a clean
tree. A `select` entry is a gate: enable a rule group only in the
change that makes it pass. Four groups are deliberately deferred
with measured counts and rationale beside the list; `BLE` is
permanently excluded with its reason.

When iterating, scope formatters to touched files
(`uv run ruff format $(git diff --name-only -- '*.py')`) rather than
the whole tree, so unrelated drift does not land in your diff.

## Where code goes

One concern per file. A reader opening a module should be able to
name its job in a sentence; if the answer needs an "and", it wants
splitting into a package whose `__init__.py` re-exports the public
surface so callers do not shift.

- CLI subcommands: `mimir/cli/<group>.py`, wired into
  `register_cli(app)` in `mimir/cli/__init__.py`. Admin subgroups
  under `mimir/cli/admin/`.
- Web routes: `mimir/web/routes/<family>.py`, attached to `bp_web`
  from `mimir.web._blueprint`. Templates in `mimir/templates/`,
  shared URL helpers in `mimir/web/urls.py`, Jinja filters in
  `mimir/web/filters.py`, request hooks in `mimir/web/hooks.py`.
- Body to HTML: `mimir/rendering/` (`blocks` segmentation, `diff`
  hunk rendering, `linkify`, `body` orchestrator).
- SEO surfaces: `mimir/seo/` (`json_ld`, `sitemaps`, `atom`).
- Threading: `mimir/threading.py`.
- Ingest: `mimir/ingest/`.
- Schema: `mimir/models.py` plus an alembic revision.

**Check the surrounding package before a non-trivial edit inside
one.** Local conventions (what is re-exported, dispatch tables,
where shared helpers live) sit in `__init__.py` and the siblings,
not in the file you are about to change. Non-trivial means adding,
removing or renaming a top-level symbol, changing a public
signature, wiring a new route or CLI command, or the first time you
touch an unfamiliar package.

## Conventions that are load-bearing

- **Git is the source of truth.** Never persist body, full headers
  or attachment content into SQLite.
- **One MIME parser path.** Everything goes through
  `mimir.parser.parse_message`. Do not add a second.
  `parse_message` must stay importable at module level so it
  pickles to worker processes for parallel ingest.
- **Config is env-driven through the pydantic `Settings` class** in
  `mimir/config.py`. New tunables get a default on the field and an
  env override; callers read `settings.foo` rather than re-parsing
  env. Note `list[str]` fields take a JSON array in env
  (`'["a","b"]'`), not comma-separated values.
- **Comments explain why, not what.** Document the reasoning.
- **No em-dashes or double-dashes in prose**, anywhere: code
  comments, docstrings, commit messages, PR bodies, CHANGELOG.
  Use commas, colons, parentheses, or restructure.
- **KISS, and write lazy-first.** Do not add indirection until a
  second caller exists. Prefer the standard library to custom code
  and a native platform feature to a new dependency.

## Whoever builds an identifier and whoever accepts it share one predicate

This is the defect class that recurs here more than any other, so it
is worth knowing before your first change rather than after.

One module decides a URL (or a cache key, or a slug, or an ID) and
another module decides whether to serve it. When those are parallel
implementations rather than one shared function, they drift, and the
drift is invisible until something starts producing at volume. A
sitemap is the usual trigger: a crawler then walks the disagreement
systematically instead of a reader stumbling into it once.

Recorded instances, all real:

- A path helper percent-encoded any subsystem name, while the route
  rejected control bytes. Production carried three MAINTAINERS
  section titles containing a TAB, so three inboxes were about to
  publish dead URLs to crawlers.
- A page took its canonical from Werkzeug's URL-DECODED
  `request.path` while the sitemap emitted the percent-encoded form,
  so nearly every page advertised one URL and self-nominated another.
- A link gated on "is this address safe to display" when the question
  that mattered was "does this page exist". Both predicates were
  real and true; only one was relevant.

**The test is to ask who else decides this is valid, and then make
that one function.** A link, a sitemap entry, and the target page's
own canonical must come out BYTE-identical, not equivalent after
normalisation, because the normalisation is the crawler's to do and
it is charged as a duplicate-URL signal.

### A cache validator is the same pair, in time

An ETag or `Last-Modified` is the emitter/acceptor rule aimed at the
future: the code deciding a response and the code deciding whether a
cached copy is still that response must agree on what the response
depends on. So for any surface with a validator, **enumerate every
input its rendered output depends on and check the validator carries
all of them**, configuration knobs included.

Config knobs are the ones that get missed, because an env change
restarts the same image and `mimir.__version__` does not move, so
nothing else in the validator shifts either. Both message and thread
pages are `Cache-Control: public, no-cache`, which makes the ETag the
only thing standing between a change and every CDN edge plus every
conditional-GET crawler serving a body the data has contradicted.

## Fix the class, not the reported instance

A reported defect is a sample. Before you call it fixed, ask what
else is in its category and check each one; a fix that closes the
reported instance and leaves its siblings is the characteristic
failure mode of fix-code, and it is not caught by testing, because
what you wrote is locally correct.

Two defects found in review of the 3.10.0 release were siblings of
fixes made in that same release:

- Sitemap caches were keyed by the thread render cap. The message
  page's ETag depended on the same cap and was missed, so changing
  the cap left caches and crawlers holding a page whose canonical
  named a thread page that 404s.
- Maintainer links were aligned with the maintainer route's
  validation. Reviewer links in `subsystem.html` stayed gated on a
  different predicate than the reviewer route accepts, until #611 in
  3.11.0.

Same with prose. When you correct a claim, grep for every other copy
of it: a stale claim about warm-cache tiers was corrected in four
places in one release and a fifth copy survived. Use
`grep -rn --exclude-dir=.git`, not `git grep`, because some
documentation in this tree is deliberately untracked and `git grep`
cannot see it.

## Never trust a scale figure in a doc, comment or docstring

Re-measure it. These numbers are a floor with a date attached, never
a fact, because the data only grows. Before deriving anything from
one (a timeout, a batch size, a cache TTL, whether something fits
under a protocol limit), measure it against the real corpus.

Two failure modes, both of which have happened here: the figure was
stale by an order of magnitude, and the figure measured a PART and
was read as the WHOLE (a per-inbox count mistaken for the archive,
understating it 5x). A wrong scale figure is invisible to review by
construction, because reviewers check claims against the model of
production they are handed rather than against production.

When you write a number down, write what it counted and when, next
to the code that depends on it. When you CORRECT a stale number,
either measure the replacement or say plainly that it is unmeasured.
Never scale the old one: a fresh invented figure is worse than the
stale one it replaces, because its date vouches for it.

## Tests

`uv run pytest` is the primary gate and runs in about two minutes.
When changing parser logic, the cache encoder, dashboard helpers,
canonical resolution, rendering, or route shapes, **add a test**
rather than relying on manual checks: a regression in those layers
silently corrupts cached data or breaks URL contracts.

Five checks that are worth more than "write a good test", because
each has caught a guard that looked fine and guarded nothing:

1. **Watch it fail.** Run the assertion against the unfixed code and
   see red before fixing. A test never seen to fail is not a guard.
2. **Assert the fixture's precondition.** State the condition the
   test exists for and check the fixture actually built it.
3. **Count executions** when the central assertion sits behind an
   `if` or a `continue`. Assert the count, per parametrisation.
4. **Ask what the broken state looks like** and whether your
   comparison degenerates there. An assertion that both sides agree
   proves nothing if the bug makes them agree.
5. **Parse values; never `in` against formatted output.**
   `"remaining=5" in log` also matches `remaining=50`, and
   `"inboxes=0"` occurs inside `"split_inboxes=0"`. Use
   `re.search(r"\bfield=(\d+)")` and compare the parsed value.

Note the suite installs a broker context session-wide, so a code
path that can only work inside the broker still passes its tests.
If you are testing something that writes, ask whether the process it
really runs in has a writer.

## CHANGELOG

**Add a line under `## [Unreleased]` in the same PR as the change**,
for any user-visible behaviour, schema, config, or CLI/route shape
change. Skip it only for pure internal refactors.

This is the convention most often missed by agents that cannot see
it, which is why this file exists. Entries are written when the work
lands and become the published release notes an operator sizes a
deploy window from, so they are a live surface rather than a record:
if an entry quotes a measured figure the code later corrected, fix
the entry too.

Categories: **Added**, **Changed**, **Deprecated**, **Removed**,
**Fixed**, **Security**.

## README currency

Before opening a PR, sweep the README files the change touches or
invalidates. User-facing means: command examples, env vars and flag
names, deploy ordering and operator-facing behaviour, project-layout
listings, route lists, status and feature claims, hardcoded versions
and example URLs.

In particular, **adding or renaming a public route means updating
the route list**, and removing or renaming any structural identifier
(a module, a workflow, a Settings field, a CLI subcommand) means
grepping the README for the old name in the same PR. Internal
refactors that touch no user-facing surface need no README change.

## Pull requests

- **One closing keyword per issue.** `Closes #N, #M.` closes only
  `#N` and silently leaves `#M` open. Write `Closes #N. Closes #M.`
  and put the list in the PR body, which is the surface that works
  across merge strategies.
- Keep the diff truthful to its title. If unrelated drift appears in
  the working tree, revert it rather than bundling it.
- Branch names follow git-flow: `feature/*`, `fix/*`, `chore/*`,
  `docs/*`, `refactor/*`, `test/*` off `develop`.

## Branching and versioning

git-flow. `develop` is the default branch and the integration
target; `main` holds tagged released code only. Feature work
branches off `develop`; releases go through `release/X.Y.Z`; urgent
fixes through `hotfix/X.Y.Z` off `main`.

The version source of truth is `pyproject.toml`'s `version`, read at
runtime via `importlib.metadata` and surfaced in the web footer.
`uv.lock` must be refreshed in the same commit, or `develop`
diverges from `main` on the next clone. Both are bumped on the
release branch, not on feature branches.

SemVer by what changed: PATCH for fixes and dependency refreshes,
MINOR for behaviour, UX or new config, MAJOR for schema or interface
breaks.

CI is the gate on pull requests (`lint`, `test`, `test-ft`, a docker
build-verify). Branch pushes are not triggers. A `v*` tag push is
what builds and publishes the image, and it runs no tests: it trusts
the green PR the tag sits on.

## If you are unsure

Ask rather than guess, particularly about anything touching the
single-writer invariant (every SQLite write goes through the
`mimir-broker` process; every other process opens
`PRAGMA query_only=1`), the redaction posture for email addresses,
or production scale.
