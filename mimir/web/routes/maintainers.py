"""Global (cross-inbox) maintainer profile page.

Unlike every other route in this package, `/maintainers/<address>` has
no `<inbox_name>` segment: a maintainer's MAINTAINERS entry and their
review-trailer activity both span every configured inbox, so the page
is a site-level surface, not a per-inbox one. `current_inbox` is
passed as `None` to `render_template`, which base.html already treats
as falsy (no inbox nav links rendered, no atom-feed `<link>`).
"""

from flask import abort, render_template

from mimir.extensions import SessionLocal
from mimir.maintainer_directory import (
    MaintainerProfile,
    is_addressable_maintainer_address,
    maintainer_path,
    maintainer_profile,
)
from mimir.web._blueprint import bp_web
from mimir.web.urls import _site_base


def _json_ld_maintainer(profile: MaintainerProfile, canonical_url: str) -> dict:
    """schema.org `ProfilePage` for `/maintainers/<address>`. Minimal
    by design (name + url only): maintainers are allowlisted by
    construction (every address here came from the kernel tree's
    MAINTAINERS file), so there's no redaction posture to thread
    through the way `_json_ld_message`'s author handling has to."""
    return {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "name": f"{profile.name or profile.address} | Maintainers",
        "url": canonical_url,
        "mainEntity": {
            "@type": "Person",
            "name": profile.name or profile.address,
            "url": canonical_url,
        },
    }


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
    if not is_addressable_maintainer_address(address):
        abort(404)
    address_normalized = address.lower()
    with SessionLocal() as session:
        profile = maintainer_profile(session, address_normalized)
        if profile is None:
            abort(404)

    base = _site_base()
    canonical_url = base + maintainer_path(address_normalized)
    return render_template(
        "maintainer.html",
        profile=profile,
        canonical_url=canonical_url,
        page_json_ld=_json_ld_maintainer(profile, canonical_url),
        current_inbox=None,
    )
