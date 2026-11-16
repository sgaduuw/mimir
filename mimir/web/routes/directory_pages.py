"""Global, letter-paged MAINTAINERS directories with cache-only activity."""

from flask import abort, redirect, render_template, request

from mimir.directories import maintainer_directory_rows, subsystem_directory_rows
from mimir.directory_paths import (
    DIRECTORY_LETTERS,
    directory_letter,
    maintainer_directory_path,
    subsystem_directory_path,
)
from mimir.extensions import SessionLocal
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


def _directory(kind: str, letter: str | None):
    path_for = (
        subsystem_directory_path if kind == "subsystems" else maintainer_directory_path
    )
    with SessionLocal() as session:
        rows, available = (
            subsystem_directory_rows(session)
            if kind == "subsystems"
            else maintainer_directory_rows(session)
        )
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
    selected = (
        [row for row in rows if directory_letter(row["name"]) == letter]
        if letter
        else []
    )
    selected = sorted(
        selected,
        key=lambda row: (row["name"].casefold(), row.get("address", row["name"])),
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
    metric = "message_count" if kind == "subsystems" else "review_count"
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
            }
            for value in DIRECTORY_LETTERS
            if value in occupied
        ],
        show_rows=letter is not None,
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
