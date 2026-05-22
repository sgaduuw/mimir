"""IndexNow push-notification client.

IndexNow (https://www.indexnow.org/) lets a site proactively notify
participating search engines about new or changed URLs instead of
waiting for the next crawl. Bing, Yandex, Naver, Seznam, and Yep
consume it; Google does not (as of 2026-05). We use it from the
`update` scheduler tick to push the canonical URLs of articles that
were just ingested.

Off-by-default: `notify` and the route registration in `web.py` are
both no-ops unless `settings.indexnow_key` is set. See
`Settings.indexnow_key` for the operator-facing knobs.

`build_urls` is the message-IDs-to-canonical-URLs bridge, it lives
here, not in `cli.py`, so the CLI seam stays small. The canonical-
pick + URL-format helpers live in `mimir.web` (single source of
truth for the rule that decides which inbox owns a cross-post); we
import them here rather than re-implementing.
"""

import json
import logging
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request

from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir._outbound import OUTBOUND_OPENER
from mimir.config import settings
from mimir.models import Article, ArticleList, Inbox
from mimir.web import _canonical_url_for

logger = logging.getLogger(__name__)

# IndexNow's documented per-request URL ceiling. We cap separately
# at `Settings.indexnow_max_per_tick` (skip-on-backfill behaviour),
# but the protocol limit is the hard upper bound regardless.
INDEXNOW_MAX_URLS_PER_REQUEST = 10000

# Short timeout so a flaky IndexNow endpoint doesn't stall the
# scheduler tick. The push is best-effort; the sitemap is the
# durable signal.
INDEXNOW_TIMEOUT_S = 5.0


def build_urls(session: Session, message_ids: list[str], base: str) -> list[str]:
    """Translate a list of newly-ingested message IDs into a list of
    canonical absolute URLs ready for the IndexNow `urlList` payload.

    Resolves the canonical inbox per article (preferring
    `Article.canonical_inbox_id`, falling back to the alphabetically-
    first linked inbox) and composes `<base>/<inbox>/<YYYY>/<MM>/<id>`.
    Articles with no linked inbox (corrupt rows, shouldn't happen) or
    no date are skipped silently, the alternative is a dangling URL
    in the push, which is worse than a missed notification.

    Two queries: one for Articles, one for their ArticleList joins.
    Both bounded by len(message_ids), which is capped above by the
    `update` caller, no risk of pulling the full archive.
    """
    if not message_ids:
        return []
    articles = list(
        session.execute(
            select(Article).where(Article.message_id.in_(message_ids))
        ).scalars()
    )
    if not articles:
        return []
    # One bulk query for the (article_id, inbox_id, inbox_name)
    # joins so we don't N+1 the inbox lookup per article.
    rows = session.execute(
        select(ArticleList.article_id, Inbox.id, Inbox.name)
        .join(Inbox, Inbox.id == ArticleList.inbox_id)
        .where(ArticleList.article_id.in_([a.id for a in articles]))
    ).all()
    links_by_article: dict[int, list[tuple[int, str]]] = {}
    for article_id, ix_id, ix_name in rows:
        links_by_article.setdefault(article_id, []).append((ix_id, ix_name))

    base = base.rstrip("/")
    urls: list[str] = []
    for article in articles:
        if article.date is None:
            continue
        links = links_by_article.get(article.id, [])
        if not links:
            continue
        # Canonical-pick + path-format lives in `mimir.web`; this
        # call yields the same string the web route would render
        # as `<link rel="canonical">`, so IndexNow pushes match
        # the URL search engines see on subsequent crawls.
        url = _canonical_url_for(article, links, base=base)
        if url is not None:
            urls.append(url)
    return urls


def notify(urls: list[str]) -> int:
    """POST `urls` to IndexNow. Returns the number of URLs that were
    submitted (0 on no-op, no key, no base URL, no URLs, or any
    network/HTTP failure, we never raise to the caller).

    No-ops silently when `settings.indexnow_key` or
    `settings.site_base_url` is unset, so the scheduler can call
    this unconditionally and not worry about config state.

    Splits into chunks of INDEXNOW_MAX_URLS_PER_REQUEST so a tick
    that lands right at `indexnow_max_per_tick` doesn't fail the
    protocol-level limit. (1000 default < 10000 limit so chunking
    won't kick in under any sane config, but the loop is the
    correct shape if `indexnow_max_per_tick` is ever raised.)
    """
    if not urls:
        return 0
    key = settings.indexnow_key
    base = (settings.site_base_url or "").rstrip("/")
    if not key or not base:
        if urls and not key:
            logger.debug("indexnow: skipping push, no key configured")
        if urls and not base:
            logger.warning(
                "indexnow: key set but site_base_url empty, cannot build "
                "keyLocation, skipping push"
            )
        return 0

    host = urlparse(base).netloc
    if not host:
        logger.warning("indexnow: site_base_url %r has no host, skipping", base)
        return 0
    key_location = f"{base}/{key}.txt"

    submitted = 0
    for i in range(0, len(urls), INDEXNOW_MAX_URLS_PER_REQUEST):
        chunk = urls[i : i + INDEXNOW_MAX_URLS_PER_REQUEST]
        payload = {
            "host": host,
            "key": key,
            "keyLocation": key_location,
            "urlList": chunk,
        }
        req = Request(
            settings.indexnow_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            # Hardened opener (refuses 3xx; see `mimir/_outbound.py`)
            # so a redirecting endpoint can't bounce the POST body
            # (which carries the IndexNow key) to an attacker host.
            with OUTBOUND_OPENER.open(req, timeout=INDEXNOW_TIMEOUT_S) as resp:
                status = resp.status
                # IndexNow accepts both 200 (received and processed) and
                # 202 (received, processing async). Other codes are
                # documented as errors; log but don't raise.
                if status in (200, 202):
                    submitted += len(chunk)
                    logger.info(
                        "indexnow: pushed %d URL(s), status=%d",
                        len(chunk),
                        status,
                    )
                else:
                    logger.warning(
                        "indexnow: unexpected status %d for %d URL(s)",
                        status,
                        len(chunk),
                    )
        except URLError as exc:
            logger.warning(
                "indexnow: network error pushing %d URL(s): %s",
                len(chunk),
                exc,
            )
        except Exception as exc:
            # Any other surprise (JSON encoding, socket weirdness)
            # log and keep going. The whole feature is best-effort;
            # the sitemap is the durable discovery path.
            logger.warning(
                "indexnow: unexpected error pushing %d URL(s): %r",
                len(chunk),
                exc,
            )
    return submitted
