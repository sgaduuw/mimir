# Web interface

[README](../README.md) · [Managing the archive](archive.md) · [Operations](operations.md)

Mimir serves HTML with Flask and Jinja. HTMX 4 enhances navigation and loading;
thread pagination also works with ordinary links. The interface is read-only.

## Find and read messages

Start at `/` to choose an inbox. Its dashboard shows recent messages, active
threads, pull requests, release announcements, configured author trackers, and
archive statistics. Active threads and compact recent messages share the main
column; pulls, release chips, configured author lists, subsystem activity and
history stack in a sidebar. The header shows cached archive totals, the first
message date, 30-day UTC calendar volume and latest activity. Active roots keep
meaningful lifecycle badges and seven-day reply counts; Recent has no badges,
including messages appended with Load more. Browse by day or month, or search
subjects and authors.
Search is substring-based rather than full-text body search; broad misses can
be expensive on a cold archive. The 100 newest matching messages are grouped by
inbox thread root, using the newest match's subject and time. Each row shows
matching sender names and a linked matching-message count, rather than the
thread's lifetime size. Multiple matching patches of the displayed posting
show their additional positions. The cap applies before grouping; refine the
query to find older matches.

A message page shows a mail header, body and attachments, followed by its
thread overview. The context bar links to the whole thread and offers
Previous and Next controls. Overview rows show the sender and date; subjects
appear only when they differ from the thread's topic after reply-prefix
normalization. These controls and the thread overview keep
working without JavaScript; with HTMX, navigation replaces the message and
scrolls back to its header. In a
thread longer than one thread-view page, the tree shows the part around the
message (its ancestors, nearest sibling replies and first replies) and links to
the thread view for the rest. Whole-thread
views display messages in chronological order with `THREAD_VIEW_RENDER_CAP`
messages per page (default 75, minimum 1). Replies link back to the thread root. Cross-posted
lists may have different conversation membership, so thread views remain
inbox-specific.

Quoted replies fold as nesting grows. Patch bodies have highlighted additions
and removals, hunk anchors such as `#h-2`, and line anchors such as `#h-2-L15`.
Message-ID references link to archived messages; lore links can include a local
mirror link. Text attachments have a preview where supported.

On the whole-thread view, a plain-text
or quoted block of more than 40 lines shows its first 40, then a "more lines"
link that loads the message's full body in place (it is the message page's
text, so it works without JavaScript too). Patch bodies there keep their
`+`/`-` colouring but not per-language syntax colours or line anchors.

## Follow patch and review activity

The navigation's theme selector offers System, Light and Dark. System follows
your device preference; Light and Dark are remembered in this browser. On a
phone within an inbox, open the navigation menu to reach the selector. Without JavaScript,
the site follows your system theme.

Thread pages show review trailers, maintainer attestations, tracked-tree landing
records, revision history, and links to compare patch revisions. Each message
also has a details disclosure for its own status, subsystem attribution and
related content. Details load when opened, with a link for readers without
JavaScript. Single-message threads have these paths too. Revision and diff
links remain in the thread page's HTML. These depend on
the archived messages and configured [tree tracking](archive.md#track-kernel-trees-and-maintainers).

Subsystem dashboards lead with reviewed patches awaiting pickup, then active
threads and the 30 newest postings or standalone patches matching the inbox's
MAINTAINERS path rules. Each posting expands to at most 30 matching patch
positions, with truncation stated. Cover lifecycle progress describes the full
posting across inboxes and paths, separately from its matching count. Quiet
patches start collapsed; ownership, file inclusions/exclusions and reviewer
activity sit in a sidebar that stacks on smaller screens.

The subsystem index lists addressable active subsystems in activity order,
with seven-day message counts, proportional bars, maintainer summaries and
last-activity dates. Its columns stack on phones. It can be empty before a
slow cache-warming pass; a request never runs the activity aggregation.
Maintainer profiles collect activity across inboxes for each MAINTAINERS `M:`
address. Their header shows initials and subsystem/inbox totals; lowercase
subsystem cards carry status pills and stack on phones. Cards have no links
because this page has no selected inbox. Review chips link to each inbox's
reviewer listing. When cached counts are available, chips sort by descending
count with alphabetical ties; otherwise they stay alphabetical without counts.
Counts cover all indexed review-trailer events: multiple roles on one message
count separately, and cross-posts contribute to each inbox. They are not counts
of unique patches or people.

## Page and feed reference

All entries below are GET routes. `<inbox>` is its configured name; `<id>` is
an indexed article ID. Message dates in URLs must match the indexed date.
A message URL under a list the message is not filed in redirects (301) to
its canonical message URL; a wrong date or an unknown list still returns 404.

| Path | Purpose |
| --- | --- |
| `/` | Header figures, a Message-ID lookup form, the busiest inboxes this month, active subsystems this week, and every inbox on one line (filter and sort need JavaScript) |
| `/<inbox>/` | Inbox dashboard |
| `/<inbox>/today`, `/<inbox>/yesterday` | UTC daily views, 100 threads per page (`?page=2`) |
| `/<inbox>/since/<date>` | Activity since an ISO date (`YYYY-MM-DD`), limited to the last 90 days |
| `/<inbox>/<YYYY>/`, `/<inbox>/<YYYY>/<MM>/` | Year and month archives |
| `/<inbox>/search?q=<query>` | Search subject and author |
| `/<inbox>/author/<substring>` | Author activity |
| `/<inbox>/reviewer/<address>` | Review activity, including trailers under retired addresses the kernel's `.mailmap` maps to it; a retired address redirects (301) to the canonical one when that is a linkable address |
| `/m/<message-id>` | Redirect to the canonical message URL |
| `/r/<message-id>`, `/all/<message-id>/` | Accept pasted lore shapes through the same global Message-ID lookup; input-only aliases, never emitted by the site |
| `/m/?id=<text>` | The same lookup for a GET form. Accepts a bare ID, `<id>`, `mid:<id>`, or a lore-style URL or path ending in the ID; a miss is a 404 |
| `/<inbox>/m/<message-id>` | Message lookup restricted to an inbox |
| `/<inbox>/<YYYY>/<MM>/<id>` | Message and thread tree |
| `/<inbox>/<YYYY>-<MM>-<id>`, `.../<YYYY>-<MM>-<id>/t` | Recovery aliases redirect (301) to the existing slash-form message or thread route |
| `/<inbox>/<YYYY>/<MM>/<root-id>/t` | First page of a whole thread |
| `/<inbox>/<YYYY>/<MM>/<root-id>/t/<page>` | Subsequent thread page |
| `/<inbox>/<YYYY>/<MM>/<id>.md`, `.../<root-id>/t.md`, `.../<root-id>/t/<page>.md` | The message page or a thread page as Markdown |
| `/<inbox>/<YYYY>/<MM>/<id>/attachment/<n>` | Download attachment |
| `/<inbox>/<YYYY>/<MM>/<id>/attachment/<n>/preview` | Preview attachment |
| `/<inbox>/series/<key>/diff?from=<vN>&to=<vM>&pos=<N>[&s=<head>]` | Compare a patch position across revisions; `pos=cover` compares cover letters; `s` names a series split off its key (unrelated postings under one cover title), absent for the key's first series |
| `/<inbox>/subsystem/` | Active subsystem index |
| `/<inbox>/subsystem/<name>/` | Subsystem dashboard |
| `/maintainers/<address>` | Cross-inbox maintainer profile |
| `/merge-window` | Patches in linux-next not yet in Linus's tree, by kernel subsystem; linked commits only |
| `/<inbox>/feed.atom` | Inbox Atom feed |
| `/<inbox>/author/<substring>/feed.atom` | Author Atom feed |
| `/api/<inbox>/recent?offset=N` | HTML partial for loading more recent messages; offset capped at 1,000 |
| `/api/<inbox>/metadata/<id>` | HTML partial: per-message status, subsystem attribution, revisions and related content, reached from thread cards; `noindex`, `no-store` |
| `/api/<inbox>/related/<id>` | HTML partial: other recent patches touching the same files, loaded when a patch is opened in a thread page's Patches tab; `noindex` |

The lore aliases preserve the shared Message-ID normalizer and unknown-ID lookup
form. `/all/<message-id>` also accepts an omitted terminal delimiter. The all alias
consumes exactly one terminal delimiter; an ID that itself ends with a slash
therefore needs two terminal slashes. Other ID slashes are preserved. Exact
inbox names `r` and `all` are reserved for these recovery inputs.

Hyphenated date aliases only build the existing slash-form route with `url_for`.
That first hop performs no database lookup and drops unrelated query arguments.
The destination owns date and inbox validity: wrong dates remain 404, message
pages can recover a wrong inbox through their existing canonical redirect,
and thread pages still require membership in the selected inbox. Linked
cross-posts retain the destination view's existing inbox-specific behavior.
These aliases are accepted inputs, never another spelling emitted by links or
sitemaps. There are no hyphenated Markdown or paginated-thread aliases.

Daily, month and since listings accept `?filter=patches`, `?filter=unanswered`
or `?filter=pulls`; omitting the filter shows all threads. Unanswered means
no replies in the selected window. Filtering precedes display caps. Daily
paging preserves the filter, for example `/lkml/today?filter=patches&page=2`.
Invalid filters/pages and pages beyond the result set return 404. Month shows
at most 100 matching threads; since shows at most 200. Author and reviewer
listings remain message lists, without these thread filters.

Today, yesterday and since are noindex with no canonical on every page.
Filtered month views are also noindex; links remain followable. Unfiltered
month archives retain their archive indexing policy.


Unsupported maintainer addresses remain visible as text without profile links.
Unknown or unsupported profile addresses return 404.

### Series comparisons

A comparison missing `from`, `to` or `pos` redirects (301) to an existing
cover conversation: `to` when supplied, otherwise `from`, otherwise the
earliest revision. Canonical inbox and containing thread page are retained;
a message page is used when the thread cannot safely be advertised. Unknown
series, heads or supplied revisions, self-diffs and invalid position/head
values still return 404. The site emits complete comparison URLs.

Series diff pages compare full message bodies, including commit explanations.
Previous/Next and the position selector visit only resolvable comparisons.
The selector's GET form works without JavaScript and preserves the exact
`from`, `to` and optional `s` values; JavaScript follows the selected canonical
URL automatically. With HTMX available, diff links, the selector and Go replace
the main content without reloading the document. The inbox navigation, title,
canonical and description follow the displayed comparison, including Back and
Forward history restores. Failed requests leave the current comparison and
selected position intact. `j` and `k` move to the next and previous position, except
while typing or using a modified key. Other adjacent revision pairs keep the
current position when available, otherwise explicitly link their cover letters.

The position extent is the largest observed ordinal in either posting, not
the total claimed in a subject. The separate comparable-position count includes
the cover and can be smaller when patches are missing or ambiguous. A complete
100-patch pair has 101 choices. Sparse numbering creates no empty choices.
The overview and long diff lines scroll within their own containers on phones.
Line totals count changes in the selected unified diff; oversized comparisons
retain the notice and message links without reporting uncomputed totals.

### Markdown pages

Each message and thread page has a Markdown version at the same URL plus
`.md`, for AI agents and other tools, and names it in
`<link rel="alternate" type="text/markdown">`. The content is chosen by URL,
never by the `Accept` header. The body is the mail as text in a fenced block,
under the same address hiding as the HTML page: a hidden sender stays hidden,
and the Message-ID is left out by design. Attachments are listed as links.
A thread's Markdown pages follow its HTML pages, `t.md` then `t/<page>.md`,
linking previous and next. Each response sends
`Content-Type: text/markdown; charset=utf-8`, its own ETag with
`Cache-Control: public, no-cache`, and `Link: <...>; rel="canonical"` naming
the HTML page's own canonical, so search engines index the HTML page. Sitemaps
do not list `.md` URLs; `/llms.txt` points agents at them. They count against
a crawler's budget exactly as the HTML pages do.

## Discovery and canonical URLs

Every message points its canonical link at the thread page containing it,
including a single-message thread's. Thread pages are self-canonical and appear
in sitemaps. Incompletely materialized threads use the root message URL until
their membership can be determined.

| Path | Contents |
| --- | --- |
| `/sitemap.xml` | Index of the following sitemap surfaces |
| `/meta-sitemap.xml` | Site root, `/merge-window` and `/llms.txt` |
| `/<inbox>/sitemap.xml` | Inbox, archive pages, and active subsystems |
| `/<inbox>/<YYYY>/sitemap.xml` | A year's thread pages |
| `/<inbox>/<YYYY>/<MM>/sitemap.xml` | A month's thread pages |
| `/sitemap-maintainers.xml` | Supported maintainer profiles |
| `/sitemap-series-diffs-<h>.xml` | Cover-letter diffs between adjacent revisions of each series whose key starts with `<h>` (`0` to `f`) |

Archive sitemaps can use `sitemap-2.xml` and later pages. The index chooses a
yearly bucket only when its root count and expanded thread-page count fit the
limits; otherwise it lists monthly buckets. A root whose thread is still being
backfilled is in no sitemap until it is ranked, so a bucket of only such roots
serves an empty file. Monthly routes remain available
when the index advertises a year. The retired `/messages-sitemap.xml` and
`messages-sitemap*.xml` files answer 410. See [sitemaps.py](../mimir/seo/sitemaps.py) for
page-size and protocol bounds.

Archive sitemap files are cached at the origin by the age of the period they
cover: one hour for the open and previous period, then a week, two weeks, and
30 days once the period ended over a year ago. They also send
`Cloudflare-CDN-Cache-Control` with that TTL capped at a week. Other sitemap
origin caches last one hour. Keys include the thread render cap where
pagination depends on it. Responses have no ETag or Last-Modified validator and
send `Cache-Control: public, max-age=300` downstream. Message and thread pages
instead use ETags with revalidation.

## Site metadata and probes

| Path | Purpose |
| --- | --- |
| `/healthz` | Liveness, no database access |
| `/readyz` | Readiness, checks a database query |
| `/robots.txt` | Configured crawler rules and sitemap location |
| `/llms.txt` | Site summary for AI agents: URL shapes with their Markdown forms, sitemap, and the `Content-Signal` of `robots.txt`'s `*` stanza |
| `/security.txt`, `/.well-known/security.txt` | Security contact; 404 unless configured |
| `/privacy` | Privacy notice linked from the footer |
| `/favicon.svg`, `/og-image.png` | Site icon and social preview image |

Health and readiness responses use `Cache-Control: no-store`. The privacy
page should be reviewed against your deployment before making the site public.

### Browser navigation history

HTMX back and forward navigation refetches the full page without keeping page
copies in local or session storage. The restored body, page metadata, theme
control and keyboard controls stay aligned. HTTP errors leave the current
content and URL intact. The theme and thread-fold preferences are still
stored locally; native links and forms remain usable without JavaScript.

### Month archive edge caching

Successful month archive pages send `Cloudflare-CDN-Cache-Control` with
`max-age=600` for the current or a future month, `max-age=3600` for the
previous month, and `max-age=604800` for older months, using the UTC date.
Browser caching stays `Cache-Control: public, max-age=600`. Year archives
do not send this edge header. Filters keep their own query-string URLs;
month pages have no separate HTMX representation.

HTML needs an explicit Cloudflare Cache Rule before the edge caches it.
See [month archive cache settings](operations.md#cache-month-archives-at-the-edge)
for eligibility, origin TTL and cache-key settings. Old months can show late
mail, reply activity and template changes up to a week late. Cached hits skip
the origin crawler budget and request log.
