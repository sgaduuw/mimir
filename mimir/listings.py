"""Unfiltered window listings, compactly cached before presentation slicing."""

import base64
import json
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from mimir import cache
from mimir.datetime_utils import coerce_dt
from mimir.models import Inbox
from mimir.patch_subjects import _is_patch_subject, _is_reply_to_patch
from mimir.pull_requests import is_pull_subject
from mimir.threading import (
    ACTIVE_THREADS_CACHE_TTL_SEC,
    ActiveThread,
    _active_thread_rows,
    since_window_start,
)


@dataclass
class ListingThread(ActiveThread):
    patch_series_position: int | None
    patch_series_key: str | None


def pack_threads(inbox_name: str, rows: list[ListingThread]) -> dict:
    """Fixed v1 column order avoids repeating field names and date type tags."""
    return {
        "inbox": inbox_name,
        "rows": [
            [
                r.id,
                r.message_id,
                r.subject,
                r.author,
                r.date.isoformat() if r.date else None,
                r.recent_count,
                r.reply_count,
                r.last_activity.isoformat() if r.last_activity else None,
                r.patch_series_position,
                r.patch_series_key,
            ]
            for r in rows
        ],
    }


def unpack_threads(payload: dict) -> list[ListingThread]:
    return [
        ListingThread(
            r[0],
            payload["inbox"],
            r[1],
            r[2],
            r[3],
            coerce_dt(r[4]),
            r[5],
            r[6],
            coerce_dt(r[7]),
            r[8],
            r[9],
        )
        for r in payload["rows"]
    ]


def encode_listing_payload(payload: dict) -> dict:
    """Level 1 keeps the measured 90-day window below the existing cache limit."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return {
        "codec": "zlib-json-v1",
        "data": base64.b64encode(zlib.compress(raw, 1)).decode("ascii"),
        "raw_bytes": len(raw),
    }


def decode_listing_payload(payload: dict) -> dict:
    if payload["codec"] != "zlib-json-v1":
        raise ValueError("Unknown listing cache codec")
    size = payload["raw_bytes"]
    if type(size) is not int or size < 1:
        raise ValueError("Invalid listing decoded size")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(payload["data"], validate=True), size + 1)
    if (
        len(raw) != size
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("Invalid listing compressed size")
    return json.loads(raw)


def listing_query(
    session: Session, inbox: Inbox, start: datetime, end: datetime
) -> list[ListingThread]:
    return [
        ListingThread(
            r.id,
            inbox.name,
            r.message_id,
            r.subject,
            r.author,
            coerce_dt(r.root_date),
            r.recent_count,
            r.reply_count,
            coerce_dt(r.last_activity),
            r.patch_series_position,
            r.patch_series_key,
        )
        for r in _active_thread_rows(
            session, inbox, start, end, order_by="last_activity"
        )
    ]


def _listing(session, inbox, window, start, end, force=False):
    payload = cache.get_or_compute(
        session,
        f"listing_threads_v1:{inbox.name}:{window}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        lambda: encode_listing_payload(
            pack_threads(inbox.name, listing_query(session, inbox, start, end))
        ),
        force=force,
    )
    return unpack_threads(decode_listing_payload(payload))


def listing_for_day(session: Session, inbox: Inbox, day: date, force=False):
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return _listing(
        session,
        inbox,
        f"day:{day.isoformat()}",
        start,
        start + timedelta(days=1),
        force,
    )


def listing_for_month(
    session: Session, inbox: Inbox, year: int, month: int, force=False
):
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return _listing(session, inbox, f"month:{year:04d}-{month:02d}", start, end, force)


def listing_since(session: Session, inbox: Inbox, since: date, force=False):
    end = datetime.now(UTC)
    start = since_window_start(since, end)
    return _listing(session, inbox, f"since:{since.isoformat()}", start, end, force)


FILTERS = ("all", "patches", "unanswered", "pulls")


def filter_threads(rows: list[ListingThread], selected: str) -> list[ListingThread]:
    if selected == "all":
        return rows
    if selected == "patches":
        return [
            r
            for r in rows
            if _is_patch_subject(r.subject) and not _is_reply_to_patch(r)
        ]
    if selected == "unanswered":
        return [r for r in rows if r.reply_count == 0]
    if selected == "pulls":
        return [r for r in rows if is_pull_subject(r.subject)]
    raise ValueError("Unknown listing filter")


def warm_listing(rows: list[ListingThread]) -> None:
    """An oversized best-effort web result is a failed warm, not a warmed key."""
    if not rows:
        return
    payload = json.dumps(
        encode_listing_payload(pack_threads(rows[0].inbox_name, rows)),
        separators=(",", ":"),
    )
    if len(payload) > cache.MAX_CACHE_VALUE_BYTES:
        raise ValueError("Listing exceeds cache value limit")
