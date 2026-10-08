# Web interface

[README](../README.md) · [Managing the archive](archive.md) · [Operations](operations.md)

Mimir serves HTML with Flask and Jinja. HTMX enhances navigation and loading;
thread pagination also works with ordinary links. The interface is read-only.

## Find and read messages

Start at `/` to choose an inbox. Its dashboard shows recent messages, active
threads, pull requests, release announcements, configured author trackers, and
archive statistics. Browse by day or month, or search subjects and authors.
Search is substring-based rather than full-text body search; broad misses can
be expensive on a cold archive.

A message page shows headers, its thread tree, body, and attachments. Whole-thread
views display messages in chronological order with `THREAD_VIEW_RENDER_CAP`
messages per page (default 75, minimum 1). Replies link back to the thread root. Cross-posted
lists may have different conversation membership, so thread views remain
inbox-specific.

Quoted replies fold as nesting grows. Patch bodies have highlighted additions
and removals, hunk anchors such as `#h-2`, and line anchors such as `#h-2-L15`.
Message-ID references link to archived messages; lore links can include a local
mirror link. Text attachments have a preview where supported.

## Follow patch and review activity

Patch pages show review trailers, maintainer attestations, tracked-tree landing
records, revision history, and links to compare patch revisions. These depend on
the archived messages and configured [tree tracking](archive.md#track-kernel-trees-and-maintainers).

Subsystem dashboards use MAINTAINERS path rules to group patches and review
activity. Their index lists active subsystems; it can be empty before a slow
cache-warming pass. Maintainer profiles collect activity across inboxes.

## Page and feed reference

All entries below are GET routes. `<inbox>` is its configured name; `<id>` is
an indexed article ID. Message dates in URLs must match the indexed date.
A message URL under a list the message is not filed in redirects (301) to
its canonical message URL; a wrong date or an unknown list still returns 404.

| Path | Purpose |
| --- | --- |
| `/` | List of inboxes |
| `/<inbox>/` | Inbox dashboard |
| `/<inbox>/today`, `/<inbox>/yesterday` | UTC daily views |
| `/<inbox>/since/<date>` | Activity since an ISO date (`YYYY-MM-DD`), limited to the last 90 days |
| `/<inbox>/<YYYY>/`, `/<inbox>/<YYYY>/<MM>/` | Year and month archives |
| `/<inbox>/search?q=<query>` | Search subject and author |
| `/<inbox>/author/<substring>` | Author activity |
| `/<inbox>/reviewer/<address>` | Review activity |
| `/m/<message-id>` | Redirect to the canonical message URL |
| `/<inbox>/m/<message-id>` | Message lookup restricted to an inbox |
| `/<inbox>/<YYYY>/<MM>/<id>` | Message and thread tree |
| `/<inbox>/<YYYY>/<MM>/<root-id>/t` | First page of a whole thread |
| `/<inbox>/<YYYY>/<MM>/<root-id>/t/<page>` | Subsequent thread page |
| `/<inbox>/<YYYY>/<MM>/<id>/attachment/<n>` | Download attachment |
| `/<inbox>/<YYYY>/<MM>/<id>/attachment/<n>/preview` | Preview attachment |
| `/<inbox>/series/<key>/diff?from=<vN>&to=<vM>&pos=<N>` | Compare a patch position across revisions; `pos=cover` compares cover letters |
| `/<inbox>/subsystem/` | Active subsystem index |
| `/<inbox>/subsystem/<name>/` | Subsystem dashboard |
| `/maintainers/<address>` | Cross-inbox maintainer profile |
| `/<inbox>/feed.atom` | Inbox Atom feed |
| `/<inbox>/author/<substring>/feed.atom` | Author Atom feed |
| `/api/<inbox>/recent?offset=N` | HTML partial for loading more recent messages; offset capped at 1,000 |

Unsupported maintainer addresses remain visible as text without profile links.
Unknown or unsupported profile addresses return 404.

## Discovery and canonical URLs

Messages in a multi-message thread point their canonical link at the thread page
containing them. Single-message threads retain their message URL. Thread pages
are self-canonical and appear in sitemaps. Incompletely materialized threads use
the root message URL until their membership can be determined.

| Path | Contents |
| --- | --- |
| `/sitemap.xml` | Index of the following sitemap surfaces |
| `/meta-sitemap.xml` | Site root |
| `/<inbox>/sitemap.xml` | Inbox, archive pages, active subsystems, and recent threads |
| `/<inbox>/<YYYY>/sitemap.xml` | A year's thread URLs |
| `/<inbox>/<YYYY>/<MM>/sitemap.xml` | A month's thread URLs |
| `/sitemap-maintainers.xml` | Supported maintainer profiles |

Archive sitemaps can use `sitemap-2.xml` and later pages. The index chooses a
yearly bucket only when its root count and expanded thread-page count fit the
limits; otherwise it lists monthly buckets. Monthly routes remain available
when the index advertises a year. See [sitemaps.py](../mimir/seo/sitemaps.py) for
page-size and protocol bounds.

Sitemap origin caches last one hour and include the thread render cap
where pagination depends on it. Responses have no ETag or Last-Modified
validator and permit five minutes of downstream caching. Message and thread
pages instead use ETags with revalidation.

## Site metadata and probes

| Path | Purpose |
| --- | --- |
| `/healthz` | Liveness, no database access |
| `/readyz` | Readiness, checks a database query |
| `/robots.txt` | Configured crawler rules and sitemap location |
| `/security.txt`, `/.well-known/security.txt` | Security contact; 404 unless configured |
| `/privacy` | Privacy notice linked from the footer |
| `/favicon.svg`, `/og-image.png` | Site icon and social preview image |

Health and readiness responses use `Cache-Control: no-store`. The privacy
page should be reviewed against your deployment before making the site public.
