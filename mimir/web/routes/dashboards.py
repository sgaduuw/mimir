"""Dashboard routes: meta-index `/`, per-inbox `/<inbox>/`, and the
per-subsystem `/<inbox>/subsystem/<name>/` page.

`_fetch_recent` lives here because both `inbox_dashboard` and the HTMX
load-more endpoint (`api_recent` in `api.py`) consume it; the latter
imports it from here.
"""

import re

from flask import abort, redirect, render_template, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from mimir.dashboard import (
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    this_day_in_history,
)
from mimir.extensions import SessionLocal
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.models import Article, ArticleList, Inbox, Subsystem
from mimir.config import settings
from mimir.seo import _json_ld_index, _json_ld_inbox
from mimir.subsystems import subsystem_path
from mimir.subsystems_dashboard import (
    active_reviewers_in_subsystem,
    active_threads_in_subsystem,
    daily_volume_in_subsystem,
    most_active_subsystems_global,
    MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
    most_active_subsystems_in_inbox,
    needs_attention_patches_in_subsystem,
    quiet_patches_in_subsystem,
    recent_articles_in_subsystem,
)
from mimir.threading import active_threads
from mimir.web._blueprint import bp_web
from mimir.web.filters import _relative_time
from mimir.web.urls import (
    _get_inbox_or_404,
    _site_base,
    _year_decade_groups,
)


RECENT_PAGE_SIZE = 10


# Cap on the per-subsystem dashboard's recent-patches list.
# MAINTAINERS subsystems vary wildly in volume (BCACHEFS is busy,
# some are dormant). A flat cap keeps response size bounded and the
# rendered list readable; a future slice can paginate.
SUBSYSTEM_RECENT_PATCHES_LIMIT = 30


# Real MAINTAINERS subsystem names are quite permissive (spaces,
# slashes, parens, ampersands, commas, dots, see "ARM/AT91 SOC
# SUPPORT", "LINUX FOR POWERPC (32-BIT AND 64-BIT)"). The conservative
# guard here just rejects ASCII control bytes, NUL, CR, LF, tab, and
# every C0/C1 control codepoint. Werkzeug already %-encodes those in
# the Location header, so this is defense-in-depth rather than a
# patch for a known injection. Anything else falls to the DB lookup
# below and naturally 404s if no row matches.
_SUBSYSTEM_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]+\Z")


def _fetch_recent(session: Session, inbox: Inbox, offset: int, limit: int):
    """Fetch limit+1 recent articles in `inbox` to detect has_more cheaply.

    Filtered via EXISTS rather than JOIN: with parameterized
    `inbox_id=?`, SQLite's planner mis-prices the JOIN form and picks
    a full scan-and-sort over `article_lists`, taking seconds. The
    EXISTS form makes "walk articles by date desc, probe article_lists
    via composite PK" the obvious plan, matching what literal binds
    would have done.
    """
    in_inbox = (
        select(ArticleList.article_id)
        .where(
            ArticleList.article_id == Article.id,
            ArticleList.inbox_id == inbox.id,
        )
        .exists()
    )
    rows = (
        session.execute(
            select(Article)
            .where(in_inbox)
            .order_by(Article.date.desc().nulls_last())
            .offset(offset)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    return rows[:limit], has_more


@bp_web.route("/")
def index():
    """Meta-index: card grid of configured inboxes with per-inbox
    stats. Pinned inboxes (settings.pinned_inboxes) surface first in
    config order; the rest follow alphabetically.

    Each card carries `archive_stats` (counts + date span) plus a
    30-day `daily_volume` sparkline. Both are cached helpers; the
    per-card cost on a warm cache is one cache row read per inbox.
    """
    pin_rank = {name: i for i, name in enumerate(settings.pinned_inboxes)}
    with SessionLocal() as session:
        inboxes = session.execute(select(Inbox)).scalars().all()
        inboxes.sort(key=lambda ix: (pin_rank.get(ix.name, len(pin_rank)), ix.name))
        inbox_summaries = []
        for inbox in inboxes:
            stats = archive_stats(session, inbox)
            inbox_summaries.append(
                {
                    "name": inbox.name,
                    "stats": stats,
                    "pinned": inbox.name in pin_rank,
                    # Relative-time string for the visible "Last activity"
                    # line. None when the inbox has no messages yet (the
                    # template falls back to the empty-state copy).
                    "last_activity_rel": (
                        _relative_time(stats.last_date) if stats.last_date else None
                    ),
                    # 30-day sparkline. Always renders so cards line up
                    # vertically even on dormant inboxes (zero-filled
                    # series → flat bar row).
                    "spark": daily_volume(session, inbox, days=30),
                }
            )
        # Cross-inbox subsystem teaser. Surfaces the most active
        # subsystems across every configured inbox so a reader on
        # `/` can drill into a hot subsystem without first picking
        # an inbox. Each row carries the inbox where it's busiest.
        #
        # `compute_on_miss=False`: cache hit serves immediately,
        # cache miss serves empty. A cold compute here aggregates
        # across every configured inbox and can run for minutes on
        # a multi-hundred-inbox corpus; under request-path posture
        # it would serialise every gunicorn worker behind the same
        # compute and trip Cloudflare's 100 s gateway timeout
        # (1.36.0 production incident). The widget renders empty
        # for the window between TTL expiry and the next warm-cache
        # refresh; warm-cache keeps the row populated in normal
        # operation.
        active_subsystems = most_active_subsystems_global(
            session,
            days=7,
            limit=12,
            compute_on_miss=False,
        )
    base = _site_base()
    return render_template(
        "index.html",
        inbox_summaries=inbox_summaries,
        active_subsystems=active_subsystems,
        current_inbox=None,
        canonical_url=base + "/",
        page_json_ld=_json_ld_index(base, inboxes),
    )


@bp_web.route("/<inbox_name>/")
def inbox_dashboard(inbox_name: str):
    """Per-inbox dashboard: active threads, pulls, releases, trackers,
    history, recent, sparkline, stats."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        active = active_threads(session, inbox, days=7, limit=10)
        trackers = [
            {
                "label": label,
                "substr": substr,
                "messages": author_recent(session, inbox, substr, 5),
            }
            for label, substr in (inbox.tracked_authors or {}).items()
        ]
        pulls = latest_pull_requests(session, inbox, limit=5)
        stable = latest_stable_releases(session, inbox, limit=5)
        history = this_day_in_history(session, inbox, years_ago=5, limit=3)
        recent, recent_has_more = _fetch_recent(session, inbox, 0, RECENT_PAGE_SIZE)
        stats = archive_stats(session, inbox)
        spark = daily_volume(session, inbox, days=30)
        # Subsystem discoverability: top-N most active subsystems in
        # this inbox over the last 7 days. Cached helper, so warm-cache
        # covers steady state. Empty list when no subsystem has
        # supported globs (no MAINTAINERS ingest yet).
        #
        # `compute_on_miss=False` for the same reason as the meta-
        # index: a cold compute here scans every patch-touched path
        # in the recent window for this inbox and runs the inverted-
        # index walk over MAINTAINERS rules. Single-digit seconds on
        # a busy inbox, fine for warm-cache but request-path-blocking
        # under the gateway timeout if it lands during the TTL gap.
        active_subsystems = most_active_subsystems_in_inbox(
            session,
            inbox,
            days=7,
            limit=10,
            compute_on_miss=False,
        )
        # Collect IDs across every Article-shaped list the template
        # renders with a pill: active threads, pulls, stable releases,
        # tracker tiles, this-day-in-history, and recent. One bulk
        # SELECT covers all of them via the per-id cache layer.
        dashboard_ids: list[int] = []
        dashboard_ids.extend(t.id for t in active)
        dashboard_ids.extend(a.id for a in pulls)
        dashboard_ids.extend(a.id for a in stable)
        for t in trackers:
            dashboard_ids.extend(a.id for a in t["messages"])
        dashboard_ids.extend(a.id for a in history)
        dashboard_ids.extend(a.id for a in recent)
        lifecycle_status_by_id = lifecycle_status_for_articles(session, dashboard_ids)
    base = _site_base()
    year_decades: list[tuple[int, list[int]]] = []
    if stats and stats.first_date and stats.last_date:
        year_decades = _year_decade_groups(stats.first_date.year, stats.last_date.year)
    return render_template(
        "inbox.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        active=active,
        trackers=trackers,
        pulls=pulls,
        stable=stable,
        history=history,
        recent=recent,
        recent_has_more=recent_has_more,
        recent_next_offset=RECENT_PAGE_SIZE,
        stats=stats,
        spark=spark,
        year_decades=year_decades,
        active_subsystems=active_subsystems,
        canonical_url=f"{base}/{inbox.name}/",
        page_json_ld=_json_ld_inbox(base, inbox, active),
        lifecycle_status_by_id=lifecycle_status_by_id,
    )


@bp_web.route("/<inbox_name>/subsystem/")
def subsystem_index(inbox_name: str):
    """Browsable list of the subsystems active in this inbox.

    A crawl hub. Per-subsystem dashboards were reachable only from
    scattered chips on individual message and thread pages, so a
    crawler had to find a matching patch first to discover one at all.

    Deliberately the ACTIVE set, not the full MAINTAINERS taxonomy.
    Listing all ~3,300 sections from all ~200 inboxes would advertise
    ~660,000 URLs, the overwhelming majority of which are empty for
    that inbox: the subsystem exists globally but no patch in this
    list touches its paths. Today those URLs stay unlinked unless a
    real patch matched, which is what keeps them worth indexing at
    all; a complete index would trade that for a thin-page factory.
    The heading says "active" so the page is not claiming to be a
    complete directory.

    Reads the same cached top-N payload as the dashboard widget, with
    `compute_on_miss=False` for the same reason that one does: the
    underlying aggregation is multi-second per inbox and must never
    run on a request. Cold cache renders an empty list for at most one
    warm cycle.
    """
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        subsystems = most_active_subsystems_in_inbox(
            session,
            inbox,
            days=7,
            limit=MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
            compute_on_miss=False,
        )
        base = _site_base()
        return render_template(
            "subsystem_index.html",
            inbox_name=inbox.name,
            current_inbox=inbox.name,
            subsystems=subsystems,
            canonical_url=f"{base}/{inbox.name}/subsystem/",
        )


@bp_web.route("/<inbox_name>/subsystem/<path:name>/")
def subsystem_dashboard(inbox_name: str, name: str):
    """Per-subsystem dashboard. Surfaces the MAINTAINERS-derived
    header (name, status, M:/R: maintainers), F:/X: paths, a 30-day
    sparkline, recent patches, active threads, and active reviewers
    for articles whose diff-touched paths match the subsystem's
    globs (minus X: vetoes).

    URL is lowercased by convention. MAINTAINERS stores names in
    upper-case ASCII ("BCACHEFS"), which is fine for the operator-
    facing dashboard heading but produces shouty URLs in bookmarks
    and browser history. The route accepts any casing,
    case-insensitive-matches against `subsystems.name`, and 301s
    non-canonical (uppercase) requests to the canonical lowercase
    form. The DB row's stored casing remains the upstream-verbatim
    one and is what the H1 renders.
    """
    if not _SUBSYSTEM_NAME_RE.match(name):
        abort(404)
    with SessionLocal() as session:
        # Resolve the inbox before the case-correction redirect so a
        # request to /unknown-inbox/subsystem/UPPER/ 404s directly
        # rather than 301'ing to the lowercase form first. Saves the
        # crawler / bookmarked-URL hop on bad inbox slugs.
        inbox = _get_inbox_or_404(session, inbox_name)
        name_lower = name.lower()
        if name != name_lower:
            # `url_for` rather than f-string interpolation: `inbox.name`
            # is validated by `mimir.inboxes` on insert and `name_lower`
            # is regex-matched above, but routing the redirect target
            # through Flask's URL builder is what CodeQL recognises as
            # safe (clears alert #14, `py/url-redirection`) and reads
            # cleaner than the manual string interpolation.
            return redirect(
                url_for(
                    "web.subsystem_dashboard",
                    inbox_name=inbox.name,
                    name=name_lower,
                ),
                code=301,
            )
        subsystem = session.execute(
            select(Subsystem)
            .options(
                selectinload(Subsystem.maintainers),
                selectinload(Subsystem.paths),
            )
            .where(func.lower(Subsystem.name) == name_lower)
        ).scalar_one_or_none()
        if subsystem is None:
            abort(404)
        recent = recent_articles_in_subsystem(
            session,
            inbox,
            subsystem,
            limit=SUBSYSTEM_RECENT_PATCHES_LIMIT,
        )
        active = active_threads_in_subsystem(
            session,
            inbox,
            subsystem,
            days=7,
            limit=10,
        )
        spark = daily_volume_in_subsystem(
            session,
            inbox,
            subsystem,
            days=30,
        )
        reviewers = active_reviewers_in_subsystem(
            session,
            inbox,
            subsystem,
            days=30,
            limit=10,
        )
        needs_attention = needs_attention_patches_in_subsystem(
            session,
            inbox,
            subsystem,
            limit=10,
        )
        quiet = quiet_patches_in_subsystem(
            session,
            inbox,
            subsystem,
            limit=10,
        )
        # Subsystem page: gather IDs from every Article-shaped list
        # the template attaches a pill to. `recent`, `needs_attention`,
        # `quiet` carry `article_id`; `active` (ActiveThread) carries
        # `id`. Reviewers (ReviewerStat) is per-reviewer aggregate
        # and intentionally excluded.
        subsystem_ids: list[int] = []
        subsystem_ids.extend(t.id for t in active)
        subsystem_ids.extend(p.article_id for p in recent)
        subsystem_ids.extend(p.article_id for p in needs_attention)
        subsystem_ids.extend(p.article_id for p in quiet)
        lifecycle_status_by_id = lifecycle_status_for_articles(session, subsystem_ids)
    return render_template(
        "subsystem.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        # Explicit, not the `default_canonical_url` fallback. That
        # fallback is `_site_base() + request.path`, and Werkzeug's
        # `request.path` is URL-DECODED, so a section named
        # `ARM/AT91 SOC SUPPORT` produced a canonical containing raw
        # spaces: not byte-identical to the link and the sitemap entry
        # (which percent-encode), and not a well-formed URI either.
        # Nearly every MAINTAINERS title has a space, so this was
        # almost every page. Inert until this change set started
        # advertising these URLs in the per-inbox sitemap; a sitemap
        # <loc> whose page names a different canonical is exactly the
        # duplicate-URL signal `subsystem_path` exists to prevent.
        canonical_url=_site_base() + subsystem_path(inbox.name, name_lower),
        subsystem=subsystem,
        recent=recent,
        recent_limit=SUBSYSTEM_RECENT_PATCHES_LIMIT,
        active=active,
        spark=spark,
        reviewers=reviewers,
        needs_attention=needs_attention,
        quiet=quiet,
        quiet_days=settings.subsystem_quiet_days,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )
