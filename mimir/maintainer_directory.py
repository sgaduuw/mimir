"""Cross-inbox maintainer directory: read-only helpers backing the
maintainer index page and per-maintainer profile page.

Two surfaces:

- `all_maintainers`: every distinct `M:` maintainer parsed from the
  kernel tree's MAINTAINERS file (`subsystem_maintainers`), case-
  collapsed since the source file is not consistently cased for the
  same person across sections (e.g. "Andrea.Ho@advantech.com.tw" vs
  "andrea.ho@advantech.com.tw").
- `maintainer_profile`: one maintainer's subsystems plus cross-inbox
  review-trailer activity, keyed by lowercased address.

Both are thin fan-outs over already-indexed columns
(`subsystem_maintainers.address`, `article_trailers.address_normalized`),
cached at the same cadence as the rest of the subsystem-dashboard
family. The TTL is defined locally rather than imported from
`mimir.subsystems`, this module is a standalone concern (a cross-
inbox directory, not a per-subsystem dashboard fan-out) and doesn't
need a dependency on that package for one shared constant.
"""

from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import (
    ArticleList,
    ArticleTrailer,
    Inbox,
    Subsystem,
    SubsystemMaintainer,
)

# Matches mimir.subsystems.SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC's value;
# kept as a local constant so this module has no import-time
# dependency on the subsystems_dashboard package.
MAINTAINER_DIRECTORY_CACHE_TTL_SEC = 3600


def maintainer_path(address: str) -> str:
    """The site-relative URL for one maintainer's profile page.

    Shared so the profile route's own `<link rel="canonical">`, the
    maintainers sitemap, and the in-page links that point at it cannot
    disagree. `@` stays literal (it is legal in a path segment and the
    canonical form has always kept it), so a link built here matches the
    canonical byte for byte rather than resolving to it via a redirect
    or a duplicate-URL signal.

    The address is lowercased: MAINTAINERS is not consistently cased for
    the same person across sections, and the route keys on the
    lowercased form.
    """
    return f"/maintainers/{quote(address.lower(), safe='@')}"


@dataclass
class MaintainerProfile:
    """One maintainer's directory entry: identity plus what they
    maintain plus where they've been reviewing.

    `address` is the lowercased canonical address, the cross-inbox
    identity key. `subsystem_maintainers.address` is stored verbatim
    from MAINTAINERS and can vary in case for the same person.

    `name` is the first non-empty MAINTAINERS display name found
    across this address's `M:` entries, or "" when every entry had a
    bare address (rare but legal in MAINTAINERS).

    `subsystems` is `(subsystem_name, status)` pairs for every
    section this address is listed as `M:` on, ordered by subsystem
    name.

    `review_inboxes` is the inbox names where this address appears
    on a review-attestation trailer (Reviewed-by / Acked-by / ...),
    ordered by name. Empty when the maintainer has no indexed review
    activity, most MAINTAINERS entries are never both a maintainer
    and an active reviewer within the archived window.
    """

    address: str
    name: str
    subsystems: list[tuple[str, str]]
    review_inboxes: list[str]


cache.register("MaintainerProfile", MaintainerProfile)


def all_maintainers(session: Session, *, force: bool = False) -> list[tuple[str, str]]:
    """Every distinct `M:` maintainer, as `(address_lowercased, name)`
    pairs ordered by address.

    Grouped in SQL on `lower(address)` so case-variant duplicates from
    the source file collapse to one row; `MIN(NULLIF(name, ''))`
    picks a display name from whichever grouped entry has one
    (deterministic, alphabetically least, rather than arbitrary row
    order). Rows with an empty address are dropped: MAINTAINERS
    permits a bare `M:` line with no address (rare but legal), and
    there's no identity to key a directory entry on.

    Cached for `MAINTAINER_DIRECTORY_CACHE_TTL_SEC` under
    `maintainers:all`. The cached value is a plain
    `list[tuple[str, str]]`; it round-trips through `mimir.cache`
    without registration because the encoder has a generic tuple
    case (`{"__t": "tuple", "v": [...]}`) independent of the
    dataclass/pydantic registry, the same mechanism
    `DailyVolume.days` relies on.
    """

    def compute() -> list[tuple[str, str]]:
        addr = func.lower(SubsystemMaintainer.address)
        rows = session.execute(
            select(
                addr.label("address"),
                func.coalesce(
                    func.min(func.nullif(SubsystemMaintainer.name, "")), ""
                ).label("name"),
            )
            .where(
                SubsystemMaintainer.role == "M",
                SubsystemMaintainer.address != "",
            )
            .group_by(addr)
            .order_by(addr)
        ).all()
        return [(row.address, row.name) for row in rows]

    return cache.get_or_compute(
        session,
        "maintainers:all",
        MAINTAINER_DIRECTORY_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def maintainer_profile(
    session: Session, address: str, *, force: bool = False
) -> MaintainerProfile | None:
    """One maintainer's profile, or `None` when `address` isn't a
    known `M:` maintainer.

    `address` is expected already lowercased by the caller (the route
    lowercases the URL segment before calling in).

    Existence is checked with a small indexed lookup BEFORE touching
    the cache, so a miss never writes a cache row: `get_or_compute`
    has no "don't cache this result" escape hatch, and caching `None`
    isn't meaningful here, there'd be nothing to invalidate later when
    MAINTAINERS gains that address. Every call, hit or miss, pays for
    this existence check (a single indexed `SubsystemMaintainer`
    lookup), cheap relative to the full profile build it guards.
    """
    exists = session.execute(
        select(SubsystemMaintainer.id)
        .where(
            func.lower(SubsystemMaintainer.address) == address,
            SubsystemMaintainer.role == "M",
        )
        .limit(1)
    ).first()
    if exists is None:
        return None

    def compute() -> MaintainerProfile:
        rows = session.execute(
            select(
                Subsystem.name.label("subsystem_name"),
                Subsystem.status,
                SubsystemMaintainer.name.label("maintainer_name"),
            )
            .join(SubsystemMaintainer, SubsystemMaintainer.subsystem_id == Subsystem.id)
            .where(
                func.lower(SubsystemMaintainer.address) == address,
                SubsystemMaintainer.role == "M",
            )
            .order_by(Subsystem.name)
        ).all()
        subsystems = [(row.subsystem_name, row.status) for row in rows]
        # First non-empty display name among the maintainer's own
        # entries, in the same subsystem-name order as `subsystems`.
        name = next((row.maintainer_name for row in rows if row.maintainer_name), "")

        review_inboxes = list(
            session.execute(
                select(Inbox.name)
                .join(ArticleList, ArticleList.inbox_id == Inbox.id)
                .join(
                    ArticleTrailer, ArticleTrailer.article_id == ArticleList.article_id
                )
                .where(ArticleTrailer.address_normalized == address)
                .distinct()
                .order_by(Inbox.name)
            ).scalars()
        )

        return MaintainerProfile(
            address=address,
            name=name,
            subsystems=subsystems,
            review_inboxes=review_inboxes,
        )

    return cache.get_or_compute(
        session,
        f"maintainer:profile:{address}",
        MAINTAINER_DIRECTORY_CACHE_TTL_SEC,
        compute,
        force=force,
    )
