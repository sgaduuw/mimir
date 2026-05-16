"""`/sitemap.xml`, `/meta-sitemap.xml`, and `/<inbox>/sitemap.xml`.

The split (issue #10) replaced a single monolithic sitemap with one
global URL cap (1000) and a COALESCE join to pick the canonical inbox
per cross-posted article. Per-inbox sitemaps don't need either; each
one lists its own URLs.
"""
from flask import Response

from mimir.extensions import SessionLocal
from mimir.seo import (
    inbox_sitemap_xml,
    meta_sitemap_xml,
    sitemap_index_xml,
)
from mimir.web._blueprint import bp_web
from mimir.web.urls import _get_inbox_or_404, _site_base


@bp_web.route("/sitemap.xml")
def sitemap():
    """Sitemap index. Lists `/meta-sitemap.xml` plus one
    `/<inbox>/sitemap.xml` per configured inbox. Crawlers fetch
    sub-sitemaps independently and can skip unchanged inboxes
    between visits via the per-entry `<lastmod>`. Cached for
    SITEMAP_TTL_SEC.

    The split (issue #10) replaced a single monolithic sitemap with
    one global URL cap (1000) and a COALESCE join to pick the
    canonical inbox per cross-posted article. Per-inbox sitemaps
    don't need either, each one lists its own URLs."""
    with SessionLocal() as session:
        body = sitemap_index_xml(session, _site_base())
    return Response(body, mimetype="application/xml; charset=utf-8")


@bp_web.route("/meta-sitemap.xml")
def meta_sitemap():
    """One-entry sitemap listing `/`. Lives behind the sitemap index
    so the index stays purely a `<sitemapindex>` (which can't carry
    a `<url>` for the root directly per sitemaps.org schema). lastmod
    is the global most-recent article date."""
    with SessionLocal() as session:
        body = meta_sitemap_xml(session, _site_base())
    return Response(body, mimetype="application/xml; charset=utf-8")


@bp_web.route("/<inbox_name>/sitemap.xml")
def inbox_sitemap(inbox_name: str):
    """Per-inbox sitemap: dashboard, year + month archives that have
    messages, plus the `SITEMAP_RECENT_PER_INBOX` most-recent article
    URLs. Cached per inbox, so an ingest into one inbox doesn't
    invalidate the others' cached responses."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        body = inbox_sitemap_xml(session, inbox, _site_base())
    return Response(body, mimetype="application/xml; charset=utf-8")
