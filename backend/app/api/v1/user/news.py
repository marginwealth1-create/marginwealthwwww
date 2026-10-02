"""User-facing market news feed.

Aggregates Indian financial-news RSS feeds (Moneycontrol, Economic Times,
Livemint, Business Standard) in parallel, de-duplicates by URL/title, and
serves a unified list. No third-party API key required — every source is
free public RSS.

The response is held in a 5-minute in-process cache so the per-request
latency is ~1 ms after the first warm hit. The first call inside a 5-min
window does the parallel fetch (~600-900 ms total).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from fastapi import APIRouter, Query

from app.core.dependencies import CurrentUser
from app.schemas.common import APIResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/news", tags=["user-news"])

# Market-news RSS feeds. Each entry is (name, url). Add more sources here
# as needed — the aggregator handles arbitrary fan-out, a dead/slow feed
# just contributes nothing (4 s timeout + graceful empty on error), and
# duplicates are merged by URL/title. Every source is FREE public RSS —
# no API key required.
#
# Mix of Indian + global so the mobile/web News tab covers domestic
# equities AND the global markets that drive forex / crypto / commodity
# sentiment (the platform trades all of these via Infoway).
_FEEDS: list[tuple[str, str]] = [
    # ── India ──────────────────────────────────────────────────────────
    ("Moneycontrol", "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ("Moneycontrol Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol Markets", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Livemint", "https://www.livemint.com/rss/markets"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    # ── Global markets ─────────────────────────────────────────────────
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    # ── Crypto (Infoway crypto segment) ────────────────────────────────
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]

# 5-min response cache. Mirrors the pattern used by option_chain.py.
_CACHE: dict[str, tuple[list[dict], float]] = {}
_CACHE_TTL = 300.0  # seconds
_FETCH_TIMEOUT = 4.0

# Namespaces present in some RSS feeds (Moneycontrol uses media:).
_NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)', re.IGNORECASE)


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    cleaned = _HTML_TAG_RE.sub("", s).replace("&nbsp;", " ").replace("&amp;", "&")
    cleaned = cleaned.replace("&quot;", '"').replace("&#39;", "'")
    return _WS_RE.sub(" ", cleaned).strip()


def _extract_image(item: ET.Element) -> str | None:
    """Pluck a thumbnail URL from whatever shape the feed used.

    RSS 2.0 doesn't standardise images; publishers use `<enclosure>`,
    `<media:thumbnail>`, `<media:content>`, inline `<img>` in description,
    or an `image` tag inside the channel.
    """
    enc = item.find("enclosure")
    if enc is not None and enc.get("url"):
        return enc.get("url")
    thumb = item.find("media:thumbnail", _NS)
    if thumb is not None and thumb.get("url"):
        return thumb.get("url")
    media = item.find("media:content", _NS)
    if media is not None and media.get("url"):
        return media.get("url")
    desc = item.findtext("description") or item.findtext("content:encoded", default="", namespaces=_NS) or ""
    m = _IMG_TAG_RE.search(desc)
    if m:
        return m.group(1)
    return None


def _parse_pubdate(raw: str | None) -> str | None:
    """Best-effort RFC-822 → ISO-8601. Falls back to the raw value."""
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except Exception:
        return raw


def _parse_feed(source: str, body: bytes) -> list[dict[str, Any]]:
    """Parse an RSS XML payload into a list of normalised dicts."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        logger.warning("RSS parse failed for %s: %s", source, e)
        return []
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = _strip_html(item.findtext("title", default=""))
        url = (item.findtext("link") or "").strip()
        if not title or not url:
            continue
        summary = _strip_html(
            item.findtext("description")
            or item.findtext("content:encoded", default="", namespaces=_NS)
            or "",
        )
        # Trim long summaries — feeds occasionally embed full articles.
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "…"
        items.append(
            {
                "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
                "title": title,
                "summary": summary or None,
                "source": source,
                "url": url,
                "image_url": _extract_image(item),
                "published_at": _parse_pubdate(item.findtext("pubDate")),
                "tag": None,
            },
        )
    return items


async def _fetch_one(client: httpx.AsyncClient, source: str, url: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MarginWealthBot/1.0)"},
            timeout=_FETCH_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return _parse_feed(source, r.content)
    except Exception as e:
        logger.warning("News fetch failed for %s: %s", source, e)
        return []


async def _aggregate(limit: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_fetch_one(client, name, url) for name, url in _FEEDS),
            return_exceptions=False,
        )
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for batch in results:
        for it in batch:
            iid = it["id"]
            tnorm = re.sub(r"[^a-z0-9]+", "", it["title"].lower())
            if iid in seen_ids or tnorm in seen_titles:
                continue
            seen_ids.add(iid)
            seen_titles.add(tnorm)
            merged.append(it)
    # Newest first. Items without a parseable date fall to the end.
    merged.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    return merged[:limit]


@router.get("", response_model=APIResponse[list[dict]])
async def market_news(
    user: CurrentUser,  # noqa: ARG001 — auth gate only
    limit: int = Query(default=40, ge=1, le=80),
):
    """Aggregated Indian-market news. Cached 5 min in process."""
    key = f"news|{limit}"
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[1]) < _CACHE_TTL:
        return APIResponse(data=hit[0])
    items = await _aggregate(limit)
    if items:
        _CACHE[key] = (items, now)
    return APIResponse(data=items)
