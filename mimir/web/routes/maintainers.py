"""Global (cross-inbox) maintainer profile page.

`/maintainers/<address>` has
no `<inbox_name>` segment: a maintainer's MAINTAINERS entry and their
review-trailer activity both span every configured inbox, so the page
is a site-level surface, not a per-inbox one. `current_inbox` is
passed as `None` to `render_template`, which base.html already treats
as falsy (no inbox nav links). The profile advertises its cross-inbox feed.
"""

from flask import abort, redirect, render_template

from mimir.dashboard import FEED_WINDOW_DAYS
from mimir.datetime_utils import aware_utc
from mimir.directories import cached_subsystem_targets
from mimir.extensions import SessionLocal
from mimir.maintainer_directory import (
    MaintainerProfile,
    cached_maintainer_review_counts,
    maintainer_path,
    resolve_maintainer_profile,
)
from mimir.seo.feed_data import maintainer_feed_data
from mimir.web._blueprint import bp_web
from mimir.web.filters import _listing_name_filter, _subsystem_label_filter
from mimir.web.urls import _site_base


def _json_ld_maintainer(
    profile: MaintainerProfile, canonical_url: str, date_modified=None
) -> dict:
    """Date the newest rendered message, without claiming full change tracking."""
    name = _listing_name_filter(profile.name)
    if name == "unknown sender":
        name = "Linux kernel maintainer"
    payload = {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "name": name,
        "url": canonical_url,
        "mainEntity": {"@type": "Person", "name": name, "url": canonical_url},
    }
    if date_modified is not None:
        payload["dateModified"] = date_modified.isoformat()
    return payload


@bp_web.route("/maintainers/<path:address>")
def maintainer_view(address: str):
    """Cross-inbox profile page for one MAINTAINERS `M:` address:
    what they maintain plus which inboxes have indexed review-trailer
    activity from them (`Reviewed-by:` / `Acked-by:` / ...).

    No 301 redirect for mixed-case input, matching the reviewer
    route's posture: the `<link rel="canonical">` in the template
    handles duplicate-URL dedup, and a redirect here would be one
    more round-trip for a case that's already cheap to normalize
    server-side.
    """
    address_normalized = address.lower()
    with SessionLocal() as session:
        profile = resolve_maintainer_profile(session, address)
        if profile is None:
            abort(404)
        if profile.address != address_normalized:
            return redirect(maintainer_path(profile.address), code=301)
        review_counts = cached_maintainer_review_counts(session, address_normalized)
        subsystem_targets = cached_subsystem_targets(session)
        feed = maintainer_feed_data(session, profile.address, compute_on_miss=False)
        recent_messages = None
        if feed is not None:
            entries, urls, names, _expires_at = feed
            recent_messages = [
                {
                    "article": article,
                    "url": f"{urls[article.id]}#m{article.id}",
                    "inbox": names[article.id],
                    "date": aware_utc(article.date),
                }
                for article in entries
                if article.id in urls and article.id in names
            ][:15]

    # Missing profiles above either redirect to a live address or abort with 404.
    assert profile is not None

    # Counts and profile refresh independently. Use one snapshot for both
    # chips and the header total, including an intentionally empty snapshot.
    review_inboxes = (
        profile.review_inboxes
        if review_counts is None
        else sorted(review_counts, key=lambda name: (-review_counts[name], name))
    )
    labels = [_subsystem_label_filter(name) for name, _status in profile.subsystems]
    ownership = ", ".join(labels[:2])
    if len(labels) > 2:
        ownership += f" and {len(labels) - 2} more"
    description = "Linux kernel maintainer"
    if ownership:
        description += f" of {ownership}"
    description += ". Recent messages and review activity"
    activity_inboxes = set(review_inboxes) | {
        row["inbox"] for row in recent_messages or []
    }
    if activity_inboxes:
        description += f" across {len(activity_inboxes)} mailing lists"
    description += "."
    if len(description) > 160:
        description = description[:157].rsplit(" ", 1)[0] + "..."
    modified = max((row["date"] for row in recent_messages or []), default=None)
    base = _site_base()
    canonical_url = base + maintainer_path(address_normalized)
    return render_template(
        "maintainer.html",
        profile=profile,
        maintainer_feed_url=maintainer_path(profile.address) + "/feed.atom",
        subsystem_targets=subsystem_targets,
        review_counts=review_counts,
        review_inboxes=review_inboxes,
        canonical_url=canonical_url,
        recent_messages=recent_messages,
        feed_window_days=FEED_WINDOW_DAYS,
        profile_description=description,
        page_json_ld=_json_ld_maintainer(profile, canonical_url, modified),
        current_inbox=None,
    )
