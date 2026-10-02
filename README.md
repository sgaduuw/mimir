# Mimir

[![CI](https://github.com/sgaduuw/mimir/actions/workflows/ci.yml/badge.svg)](https://github.com/sgaduuw/mimir/actions/workflows/ci.yml)

Mimir is a self-hosted, read-only web archive for mailing lists published
through [public-inbox](https://public-inbox.org/). Browse conversations,
read patches, and follow review activity across lists such as
[LKML](https://lore.kernel.org/lkml/) and
[linux-fsdevel](https://lore.kernel.org/linux-fsdevel/).

Messages stay in local Git mirrors. SQLite holds the index; Mimir reads
message bodies and attachments from the mirrors when you open them.

[Try it locally](#try-it-locally) · [Deploy it](#deploy-it) ·
[Documentation](#documentation) · [Release notes](CHANGELOG.md)

## What you can do

- **Read conversations.** Browse daily and monthly archives, search subjects
  and authors, or read a whole thread with quoted replies folded away.
- **Review patches.** View highlighted diffs, jump to individual hunks,
  compare patch revisions, and inspect text attachments.
- **Follow development.** See active threads, patch review activity,
  subsystem ownership from MAINTAINERS, and links to tracked Git commits.
- **Archive several lists.** Cross-posted messages share an index entry,
  while each list keeps its own thread view. Updates are incremental.
- **Make the archive discoverable.** Atom feeds, sitemaps, stable message
  links, and structured metadata are included.

Mimir is designed for a single host with SQLite and a local mirror directory.
The mirrors must remain available while the site runs. The web interface
reads archives; it does not send mail or manage mailing-list subscriptions.

## Try it locally

You need **Python 3.14**, **uv**, and **Git**. These commands create a new `.env`;
keep your existing configuration if you already have a checkout.

```sh
git clone https://github.com/sgaduuw/mimir.git
cd mimir
uv sync --locked
mkdir -p instance
uv run python -c 'import secrets; print("SECRET_KEY=" + secrets.token_hex(32))' > .env
cat >> .env <<'EOF'
DATABASE_URL=sqlite:///instance/mimir.db
BROKER_SOCKET_PATH=instance/broker.sock
SITE_BASE_URL=http://127.0.0.1:5000
EOF
MIMIR_IS_BROKER=true uv run mimir broker --socket instance/broker.sock
```

The broker prepares the database and stays running. In a **second terminal**,
from the same checkout:

```sh
uv run mimir broker-ping --socket instance/broker.sock
uv run mimir run
```

Open **http://127.0.0.1:5000/**. The archive starts empty, with LKML and
linux-fsdevel configured. To download and index a list, use a third terminal:

```sh
uv run mimir update --inbox linux-fsdevel
```

This downloads the list's full available history, so it can take time and
substantial disk space. For a bounded trial with one epoch, follow
[Import a smaller sample](docs/archive.md#import-a-smaller-sample).
Keep the broker running for archive commands. Stop the web server and broker
with Ctrl-C when finished. The generated `.env`, database, and mirrors stay local.

## Deploy it

The [Compose example](compose.yaml) runs three services: the web app, a
broker that owns database writes, and a scheduler that keeps the archive current.
Use the [deployment guide](deploy/README.md) for persistent storage,
configuration, health checks, and a TLS reverse proxy.

For a public archive, set a stable secret key and canonical site URL, configure
trusted proxy hops for your actual network, and back up the database and mirrors.
See [GitHub Releases](https://github.com/sgaduuw/mimir/releases) for releases.

## Documentation

| I want to… | Read |
| --- | --- |
| Add lists, import messages, retry failures, or track kernel trees | [Managing the archive](docs/archive.md) |
| Configure caching, crawlers, maintenance, or diagnostics | [Operations](docs/operations.md) |
| Look up pages, feeds, and URL behavior | [Web interface](docs/web.md) |
| Set up containers, proxies, or access logs | [Deployment](deploy/README.md) |
| Understand the code or run checks | [Development](docs/development.md) |
| Look up environment settings and defaults | [Settings reference](mimir/config.py) |
| Report a bug or suggest a change | [GitHub issues](https://github.com/sgaduuw/mimir/issues) |

## License

[MIT](LICENSE).
