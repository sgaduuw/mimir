"""Complete directory metadata joined to cache-only weekly activity."""

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mimir.mailmap import rules_version
from mimir.maintainer_activity import maintainer_activity_rows
from mimir.maintainer_directory import (
    all_maintainers,
    is_addressable_maintainer_address,
    maintainer_path,
)
from mimir.models import Inbox, SubsystemList, SubsystemMaintainer
from mimir.subsystems import (
    _compute_rules_snapshot,
    _rules_snapshot,
    is_addressable_subsystem_name,
    subsystem_path,
)
from mimir.subsystems_dashboard.activity import _most_active_subsystems_global_full


def subsystem_directory_target(name, activity, mirrored_lists, inbox_names):
    """One target for directory and sitemap: busiest, mirrored L:, or unlinked."""
    if not is_addressable_subsystem_name(name):
        return None, None
    if activity is not None and activity.inbox_name in inbox_names:
        inbox_name = activity.inbox_name
    else:
        inbox_name = min(mirrored_lists, default=None)
    return inbox_name, subsystem_path(inbox_name, name) if inbox_name else None


def subsystem_directory_rows(session: Session) -> tuple[list[dict], bool]:
    """Read cheap rule metadata and complete warmed activity, without scanning mail."""
    version = rules_version(session)
    snapshot = _rules_snapshot(session)
    # The writer commits replacement rules before invalidating this fixed-key
    # snapshot. During that interval refresh cheap metadata, never join new
    # activity IDs onto the old taxonomy.
    if (snapshot.version or "") != version:
        snapshot = _compute_rules_snapshot()
    if (snapshot.version or "") != rules_version(session):
        return [], False
    activity = _most_active_subsystems_global_full(session, compute_on_miss=False)
    active = {row.id: row for row in activity or []}
    inbox_names = set(session.scalars(select(Inbox.name)))
    mirrors = defaultdict(list)
    for subsystem_id, inbox_name in session.execute(
        select(SubsystemList.subsystem_id, Inbox.name).join(
            Inbox, func.lower(Inbox.list_address) == SubsystemList.address
        )
    ):
        mirrors[subsystem_id].append(inbox_name)
    if (snapshot.version or "") != rules_version(session):
        return [], False
    rows = []
    for sub in snapshot.subsystems.values():
        if not is_addressable_subsystem_name(sub.name):
            continue
        item = active.get(sub.id)
        inbox_name, path = subsystem_directory_target(
            sub.name, item, mirrors[sub.id], inbox_names
        )
        rows.append(
            {
                "name": sub.name,
                "inbox_name": inbox_name,
                "path": path,
                "message_count": (item.message_count if item else 0)
                if activity is not None
                else None,
            }
        )
    rows.sort(key=lambda row: (row["name"].casefold(), row["name"]))
    return rows, activity is not None


def maintainer_directory_rows(session: Session) -> tuple[list[dict], bool]:
    """Every current M identity, including quiet and review-only maintainers."""
    version = rules_version(session)
    names = all_maintainers(session)
    # Duplicate M lines, including case variants, count one subsystem once.
    counts = dict(
        session.execute(
            select(
                func.lower(SubsystemMaintainer.address),
                func.count(func.distinct(SubsystemMaintainer.subsystem_id)),
            )
            .where(SubsystemMaintainer.role == "M")
            .group_by(func.lower(SubsystemMaintainer.address))
        ).all()
    )
    activity = maintainer_activity_rows(session, compute_on_miss=False)
    if rules_version(session) != version:
        return [], False
    reviews = {row["address"]: row["review_count"] for row in activity or []}
    rows = [
        {
            "address": address,
            "name": name or address,
            "path": maintainer_path(address),
            "subsystem_count": counts.get(address, 0),
            "review_count": reviews.get(address, 0) if activity is not None else None,
        }
        for address, name in names
        if is_addressable_maintainer_address(address)
    ]
    rows.sort(key=lambda row: (row["name"].casefold(), row["address"]))
    return rows, activity is not None


def directory_sitemap_paths(session: Session) -> list[str]:
    """The directory hubs, occupied letter pages and their chosen subsystem targets."""
    from mimir.directory_paths import (
        directory_letter,
        maintainer_directory_path,
        subsystem_directory_path,
    )

    version = rules_version(session)
    subsystems, _ = subsystem_directory_rows(session)
    maintainers, _ = maintainer_directory_rows(session)
    # Each directory is coherent alone; a reload between them must not put
    # obsolete targets into the combined sitemap. The roots always exist.
    if rules_version(session) != version:
        return sorted([subsystem_directory_path(), maintainer_directory_path()])
    return sorted(
        {
            subsystem_directory_path(),
            maintainer_directory_path(),
            *(
                subsystem_directory_path(directory_letter(row["name"]))
                for row in subsystems
            ),
            *(
                maintainer_directory_path(directory_letter(row["name"]))
                for row in maintainers
            ),
            *(row["path"] for row in subsystems if row["path"] is not None),
        }
    )
