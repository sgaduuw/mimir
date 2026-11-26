"""Dashboard routes: meta-index `/`, per-inbox `/<inbox>/`, and the
per-subsystem `/<inbox>/subsystem/<name>/` page.

`_fetch_recent` lives here because both `inbox_dashboard` and the HTMX
load-more endpoint (`api_recent` in `api.py`) consume it; the latter
imports it from here.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from flask import abort, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from mimir._inbox_name import inbox_path
from mimir.config import settings
from mimir.dashboard import (
    archive_stats,
    author_recent,
    cached_inbox_volumes,
    cached_recent_message_total,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    recent_page,
    this_day_in_history,
)
from mimir.datetime_utils import aware_utc
from mimir.extensions import SessionLocal
from mimir.inboxes import pinned_first
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.linus_pulls import linus_pulls
from mimir.maintainer_activity import most_active_maintainers
from mimir.maintainer_directory import (
    is_addressable_maintainer_address,
    maintainer_path,
)
from mimir.merge_window import merge_window
from mimir.models import Inbox, Subsystem
from mimir.seo import _json_ld_inbox, _json_ld_inboxes, _json_ld_index
from mimir.subsystems import (
    is_addressable_subsystem_name,
    subsystem_index_path,
    subsystem_path,
)
from mimir.subsystems_dashboard import (
    active_reviewers_in_subsystem,
    active_threads_in_subsystem,
    daily_volume_in_subsystem,
    most_active_subsystems_global,
    most_active_subsystems_in_inbox,
    needs_attention_patches_in_subsystem,
    quiet_patches_in_subsystem,
    recent_postings_in_subsystem,
)
from mimir.subsystems_dashboard.recent import (
    RECENT_POSTINGS_LIMIT,
    RECENT_PREVIEW_LIMIT,
)
from mimir.threading import active_threads
from mimir.web._blueprint import bp_web
from mimir.web.filters import _relative_time
from mimir.web.routes.search import SEARCH_QUERY_MAX_LEN, SEARCH_QUERY_MIN_LEN
from mimir.web.urls import (
    _get_inbox_or_404,
    _site_base,
    _year_decade_groups,
)

RECENT_PAGE_SIZE = 10


def _fetch_recent(session: Session, inbox: Inbox, offset: int, limit: int):
    """Fetch limit+1 recent articles in `inbox` to detect has_more cheaply.

    Reads through `recent_page`, which picks the query shape by inbox
    size.
    """
    rows = recent_page(session, inbox, offset, limit + 1)
    has_more = len(rows) > limit
    return rows[:limit], has_more


# Inboxes shown as "Busiest this month", by messages in the last 30 days.
# Eight: two rows of four cards, the grid's column cap (#1267).
FRONT_PAGE_BUSIEST = 8


def _inbox_summaries(session: Session) -> list[dict]:
    """One summary per configured inbox, for the front page (cards and
    header figures) and `/inboxes/`. Pinned inboxes
    (settings.pinned_inboxes) stay first in config order; the rest follow
    alphabetically. Volumes come from warmed cache rows; on a miss `spark`
    and `messages_30d` are None."""
    pinned = set(settings.pinned_inboxes)
    now = datetime.now(UTC)
    inboxes = pinned_first(session.execute(select(Inbox)).scalars().all())
    volumes = cached_inbox_volumes(inboxes)
    summaries = []
    for inbox in inboxes:
        stats = archive_stats(session, inbox)
        # 30 UTC days, today included: the window `recent_message_total`
        # shares, so the cards and the header figure agree.
        spark = volumes.get(inbox.name)
        summaries.append(
            {
                "name": inbox.name,
                "stats": stats,
                "pinned": inbox.name in pinned,
                # None when the inbox has no messages yet.
                "last_activity_rel": (
                    _relative_time(stats.last_date) if stats.last_date else None
                ),
                "spark": spark,
                "messages_30d": (
                    sum(count for _, count in spark.days) if spark is not None else None
                ),
                "active_24h": bool(
                    stats.last_date
                    and aware_utc(stats.last_date) >= now - timedelta(hours=24)
                ),
            }
        )
    return summaries


@bp_web.route("/")
def index():
    """Front page: header figures, the busiest inboxes this month, and the
    active subsystems and maintainer activity this week. Every inbox is
    listed on `/inboxes/` instead (#1267): at 203 inboxes the list was
    most of this page's bytes and links.

    Inbox volumes and the 30-day total come from warmed cache rows.
    On a miss, the page omits the corresponding figure.
    """
    with SessionLocal() as session:
        inbox_summaries = _inbox_summaries(session)
        mw = merge_window(session, compute_on_miss=False)
        pulls = linus_pulls(session, compute_on_miss=False)
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
        # Drop names the dashboard route cannot serve before the template
        # ever sees them: a row for one would link to a 404. Filtering here
        # rather than in the template also means the section's `{% if %}`
        # and its loop agree by construction: gating on the unfiltered list
        # would render a heading over an empty list whenever every active
        # name happens to be unaddressable.
        active_subsystems = [
            s for s in active_subsystems if is_addressable_subsystem_name(s.name)
        ]
        # The warmer supplies current M: identities. Recheck the profile route
        # predicate, without rebuilding the directory or scanning activity here.
        maintainer_activity = [
            {**row, "path": maintainer_path(row["address"])}
            for row in most_active_maintainers(
                session, days=7, limit=12, compute_on_miss=False
            )
            if is_addressable_maintainer_address(row["address"])
        ]
    busiest = sorted(
        (s for s in inbox_summaries if s["messages_30d"]),
        key=lambda s: (-s["messages_30d"], s["name"]),
    )[:FRONT_PAGE_BUSIEST]
    base = _site_base()
    return render_template(
        "index.html",
        inbox_summaries=inbox_summaries,
        busiest=busiest,
        active_24h=sum(1 for s in inbox_summaries if s["active_24h"]),
        recent_total=cached_recent_message_total(),
        mw=mw,
        pulls=pulls,
        merge_window_updated=aware_utc(mw.queued_walked_at).astimezone(UTC)
        if mw and mw.queued_walked_at
        else None,
        active_subsystems=active_subsystems,
        maintainer_activity=maintainer_activity,
        current_inbox=None,
        canonical_url=base + "/",
        page_json_ld=_json_ld_index(base),
    )


@bp_web.route("/inboxes/", strict_slashes=False)
def inboxes_page():
    """Every configured inbox, one line each, with find and sort controls."""
    with SessionLocal() as session:
        inbox_summaries = _inbox_summaries(session)
    base = _site_base()
    return render_template(
        "inboxes.html",
        inbox_summaries=inbox_summaries,
        current_inbox=None,
        canonical_url=base + "/inboxes/",
        page_json_ld=_json_ld_inboxes(base, [s["name"] for s in inbox_summaries]),
    )


@bp_web.route("/<inbox_name>")
def inbox_dashboard_bare(inbox_name: str):
    """Werkzeug's own trailing-slash redirect fires before any view runs,
    so every unknown root segment (`/ads.txt`, `/search`) got a 308 to a
    URL that then 404s. Check the inbox first and redirect only to a page
    that exists (#827)."""
    with SessionLocal() as session:
        _get_inbox_or_404(session, inbox_name)
    target = url_for("web.inbox_dashboard", inbox_name=inbox_name)
    if request.query_string:
        target += "?" + request.query_string.decode()
    return redirect(target, 308)


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
        # Only active rows display lifecycle badges.
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [t.id for t in active], inbox.id
        )
        base = _site_base()
        # Inside the session: it asks which active threads have a `/t`.
        page_json_ld = _json_ld_inbox(session, base, inbox, active)
    year_decades: list[tuple[int, list[int]]] = []
    if stats and stats.first_date and stats.last_date:
        year_decades = _year_decade_groups(stats.first_date.year, stats.last_date.year)
    return render_template(
        "inbox.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        search_min_len=SEARCH_QUERY_MIN_LEN,
        search_max_len=SEARCH_QUERY_MAX_LEN,
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
        canonical_url=base + inbox_path(inbox.name),
        page_json_ld=page_json_ld,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )


def _subsystem_redirect(path: str):
    if request.query_string:
        path += "?" + request.query_string.decode("utf-8", "replace")
    return redirect(path, code=301)


@bp_web.route("/<inbox_name>/subsystem/", strict_slashes=False)
@bp_web.route("/<inbox_name>/subsystems/", strict_slashes=False)
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

    Reads the complete cached ranking and displays the busiest 100, with
    `compute_on_miss=False` for the same reason that one does: the
    underlying aggregation is multi-second per inbox and must never
    run on a request. Cold cache renders an empty list for at most one
    warm cycle.
    """
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        canonical_path = subsystem_index_path(inbox.name)
        if request.path != canonical_path:
            return _subsystem_redirect(canonical_path)
        subsystems = most_active_subsystems_in_inbox(
            session,
            inbox,
            days=7,
            limit=None,
            compute_on_miss=False,
        )
        # Count and scale only rows whose shared URL predicate permits a link.
        subsystems = [s for s in subsystems if is_addressable_subsystem_name(s.name)]
        total_subsystems = len(subsystems)
        # The complete lkml page measured 888,860 bytes for 1,183 rows
        # on 2026-11-16. Bound this view, not the shared activity payload.
        subsystems = subsystems[:100]
        base = _site_base()
        return render_template(
            "subsystem_index.html",
            inbox_name=inbox.name,
            current_inbox=inbox.name,
            subsystems=subsystems,
            total_subsystems=total_subsystems,
            canonical_url=base + subsystem_index_path(inbox.name),
        )


@bp_web.route("/<inbox_name>/subsystem/<path:name>/", strict_slashes=False)
@bp_web.route("/<inbox_name>/subsystems/<path:name>/", strict_slashes=False)
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
    one; the H1 shows it through `subsystem_label` (lowercased).
    """
    if not is_addressable_subsystem_name(name):
        abort(404)
    with SessionLocal() as session:
        # Resolve the inbox before the case-correction redirect so a
        # request to /unknown-inbox/subsystem/UPPER/ 404s directly
        # rather than 301'ing to the lowercase form first. Saves the
        # crawler / bookmarked-URL hop on bad inbox slugs.
        inbox = _get_inbox_or_404(session, inbox_name)
        name_lower = name.lower()
        # `.first()`, not `.scalar_one_or_none()`: `subsystems.name` has
        # no unique constraint and the URL form is lowercased, so two
        # MAINTAINERS sections differing only in case collapse onto one
        # URL and `scalar_one_or_none()` RAISES, i.e. a 500 on a public,
        # sitemapped page. Serving one beats 404 (they are
        # near-duplicates), and `ORDER BY id` keeps the pick stable
        # across renders. Prevalence and alternatives: issue #554.
        subsystem = (
            session.execute(
                select(Subsystem)
                .options(
                    selectinload(Subsystem.maintainers),
                    selectinload(Subsystem.paths),
                )
                .where(func.lower(Subsystem.name) == name_lower)
                .order_by(Subsystem.id)
            )
            .scalars()
            .first()
        )
        if subsystem is None:
            abort(404)
        canonical_path = subsystem_path(inbox.name, subsystem.name)
        if request.path != unquote(canonical_path):
            return _subsystem_redirect(canonical_path)

        recent = recent_postings_in_subsystem(
            session,
            inbox,
            subsystem,
            limit=RECENT_POSTINGS_LIMIT,
            preview_limit=RECENT_PREVIEW_LIMIT,
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
        # A posting's cover gets the full lifecycle rollup independently of
        # its matching preview. Missing covers have no whole-posting status;
        # child previews never trigger up to 900 extra lifecycle reads.
        subsystem_ids: list[int] = []
        subsystem_ids.extend(t.id for t in active)
        subsystem_ids.extend(p.status_id for p in recent if p.status_id is not None)
        subsystem_ids.extend(p.article_id for p in needs_attention)
        subsystem_ids.extend(p.article_id for p in quiet)
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, subsystem_ids, inbox.id
        )
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
        recent_limit=RECENT_POSTINGS_LIMIT,
        preview_limit=RECENT_PREVIEW_LIMIT,
        active=active,
        spark=spark,
        reviewers=reviewers,
        needs_attention=needs_attention,
        quiet=quiet,
        quiet_days=settings.subsystem_quiet_days,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )
