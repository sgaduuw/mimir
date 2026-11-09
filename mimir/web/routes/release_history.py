"""Release history pages, with current archive links resolved at render time."""

import subprocess

from dulwich.errors import NotGitRepository
from flask import Response, abort, render_template, request

from mimir import release_history as history
from mimir.extensions import SessionLocal
from mimir.merge_window import SECTION_MIN_PATCHES
from mimir.web._blueprint import bp_web
from mimir.web.urls import _site_base


@bp_web.route(history.MERGE_WINDOW_HISTORY_PATH)
def release_history_index():
    releases = history.release_catalog()
    recorded = history.recorded_history(releases)
    return render_template(
        "release_history.html",
        listed=recorded.listed,
        checked=recorded.checked,
        release_total=len(releases),
        release_path=history.release_history_path,
        current_inbox=None,
        canonical_url=_site_base() + history.release_history_path(),
    )


@bp_web.route(history.MERGE_WINDOW_HISTORY_PATH + "/<tag>")
def release_history_detail(tag):
    if not history.is_release_tag(tag):
        abort(404)
    release = next((r for r in history.release_catalog() if r.tag == tag), None)
    if release is None:
        abort(404)
    raw_page = request.args.get("page", "1")
    if not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 9:
        abort(404)
    page = int(raw_page)
    if page < 1:
        abort(404)
    try:
        facts, links, message_ids = history.release_data(release)
    except OSError, KeyError, NotGitRepository, subprocess.SubprocessError:
        history.logger.warning("release history unavailable for %s", tag, exc_info=True)
        return Response(
            render_template(
                "release_history_detail.html",
                release=release,
                unavailable=True,
                current_inbox=None,
                canonical_url=_site_base() + history.release_history_path(tag),
            ),
            status=503,
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        )
    with SessionLocal() as session:
        rows, total = history.archived_page(session, message_ids, page)
    # Page 1 only: later pages list more messages, not another table. None
    # until the warm has filled it; a request never groups.
    subsystems = history.release_subsystems(release) if page == 1 else None
    if page > 1 and (page - 1) * history.PAGE_SIZE >= total:
        abort(404)
    return render_template(
        "release_history_detail.html",
        release=release,
        facts=facts,
        links=links,
        rows=rows,
        total=total,
        subsystems=subsystems,
        handful=history.HANDFUL_OF_REFERENCES,
        section_min=SECTION_MIN_PATCHES,
        page=page,
        previous=history.release_history_path(tag, page=page - 1) if page > 1 else None,
        following=history.release_history_path(tag, page=page + 1)
        if page * history.PAGE_SIZE < total
        else None,
        current_inbox=None,
        canonical_url=_site_base() + history.release_history_path(tag, page=page),
    )
