"""Global, letter-paged MAINTAINERS directories with cache-only activity."""

import time

from flask import abort, redirect, render_template, request

from mimir.directories import maintainer_directory_rows, subsystem_directory_rows
from mimir.directory_paths import (
    DIRECTORY_LETTERS,
    directory_letter,
    maintainer_directory_path,
    subsystem_directory_path,
)
from mimir.extensions import SessionLocal
from mimir.mailmap import rules_version
from mimir.subsystems import subsystem_index_path
from mimir.web import filters
from mimir.web._blueprint import bp_web
from mimir.web.urls import _site_base

bp_web.add_app_template_global(subsystem_directory_path)
bp_web.add_app_template_global(maintainer_directory_path)
bp_web.add_app_template_global(subsystem_index_path)

# Production 2026-11-16: 3,403 addressable subsystems require letters (>3,000).
# The full prototype for 2,072 actual maintainer rows was 1,123,160 UTF-8 bytes
# (>500 KB), so both
# directories use stable occupied-letter pages rather than one large document.
_LETTER_CONVERTER = "<any(" + ",".join(DIRECTORY_LETTERS) + "):letter>/"
_SEARCH_LIMIT = 200

# ponytail: per-process memo of the prepared rows, keyed by rules version.
# Building them took 0.212 s on production (2026-11-16) for every letter page.
# Activity moves at most once per slow warm, so five minutes of lag is
# invisible; move to the shared cache if worker count makes rebuilds matter.
_ROWS_TTL_SEC = 300
_rows_memo: dict[str, tuple[str, float, tuple[list[dict], bool]]] = {}


def _directory_rows(kind: str, session) -> tuple[list[dict], bool]:
    version = rules_version(session)
    now = time.monotonic()
    hit = _rows_memo.get(kind)
    if hit is not None and hit[0] == version and hit[1] > now:
        return hit[2]
    result = (
        subsystem_directory_rows(session)
        if kind == "subsystems"
        else maintainer_directory_rows(session)
    )
    # Pin only warmed results: empty rows are the mid-reload sentinel, and a
    # cold page should pick up activity as soon as the warm lands.
    rows, available = result
    if rows and available:
        _rows_memo[kind] = (version, now + _ROWS_TTL_SEC, result)
    return result


def _directory(kind: str, letter: str | None):
    path_for = (
        subsystem_directory_path if kind == "subsystems" else maintainer_directory_path
    )
    query = request.args.get("q", "").strip()
    if letter is not None and query:
        return redirect(
            path_for() + "?" + request.query_string.decode("utf-8", "replace"), code=301
        )
    with SessionLocal() as session:
        rows, available = _directory_rows(kind, session)
    # Partition before display redaction, just as the sitemap does: a rendering
    # policy must not move an entry between canonical letter pages.
    occupied = {directory_letter(row["name"]) for row in rows}
    if letter is not None and letter not in occupied:
        abort(404)
    canonical_path = path_for(letter)
    if request.path != canonical_path:
        query = (
            "?" + request.query_string.decode("utf-8", "replace")
            if request.query_string
            else ""
        )
        return redirect(canonical_path + query, code=301)
    searching = letter is None and bool(query)
    activity_root = letter is None and not searching
    selected = (
        rows
        if searching or activity_root
        else [row for row in rows if directory_letter(row["name"]) == letter]
    )
    if kind == "maintainers":
        prepared = []
        for row in selected:
            allowed = filters._is_allowlisted(row["address"])
            label = filters._usable_name(row["name"]) or (
                row["address"] if allowed else "unknown sender"
            )
            prepared.append(
                {
                    **row,
                    "name": label,
                    "path": row["path"] if allowed else None,
                    "search_text": (
                        label + (" " + row["address"] if allowed else "")
                    ).lower(),
                }
            )
        selected = prepared
    else:
        selected = [
            {**row, "search_text": filters._subsystem_label_filter(row["name"])}
            for row in selected
        ]
    if searching:
        selected = [row for row in selected if query.lower() in row["search_text"]]
    selected = sorted(
        selected,
        key=lambda row: (row["name"].casefold(), row.get("address", row["name"])),
    )
    search_total = len(selected)
    if searching:
        selected = selected[:_SEARCH_LIMIT]
    metric = "message_count" if kind == "subsystems" else "review_count"
    for order, row in enumerate(selected):
        row["directory_order"] = order
    if activity_root:
        selected = (
            sorted(
                (row for row in selected if row["path"] and (row[metric] or 0) > 0),
                key=lambda row: -row[metric],
            )[:50]
            if available
            else []
        )
    maximum = max((row[metric] or 0 for row in selected), default=0)
    return render_template(
        "directory.html",
        kind=kind,
        title=kind.capitalize(),
        singular=kind[:-1],
        rows=selected,
        total=len(rows),
        activity_available=available,
        maximum=maximum,
        root_path=path_for(),
        canonical_url=_site_base() + canonical_path,
        letter=letter,
        letter_label="Numbers and other"
        if letter == "other"
        else (letter or "").upper(),
        letters=[
            {
                "letter": value,
                "label": "#" if value == "other" else value.upper(),
                "path": path_for(value),
                "occupied": value in occupied,
            }
            for value in DIRECTORY_LETTERS
        ],
        activity_root=activity_root,
        show_rows=letter is not None or searching or (activity_root and bool(selected)),
        query=query,
        searching=searching,
        search_total=search_total,
        search_truncated=searching and search_total > _SEARCH_LIMIT,
        noindex=searching,
        canonical_when_noindex=searching,
        current_inbox=None,
    )


@bp_web.route(subsystem_directory_path(), strict_slashes=False)
@bp_web.route(subsystem_directory_path() + _LETTER_CONVERTER, strict_slashes=False)
def subsystem_directory(letter: str | None = None):
    return _directory("subsystems", letter)


@bp_web.route(maintainer_directory_path(), strict_slashes=False)
@bp_web.route(maintainer_directory_path() + _LETTER_CONVERTER, strict_slashes=False)
def maintainer_directory(letter: str | None = None):
    return _directory("maintainers", letter)
