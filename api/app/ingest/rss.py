"""News RSS: advisories republished as clean text. Facts are extracted; article text is not kept beyond 7 days."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx

from .text import html_to_text, relevant

UA = "DetourAI-ingest/0.4 (+https://github.com/deepakbanavathu/Detour-AI)"


@dataclass
class Item:
    ext_id: str
    ts: datetime
    title: str
    text: str
    link: str


def parse_feed(content: bytes | str) -> list[Item]:
    d = feedparser.parse(content)
    items: list[Item] = []
    for e in d.entries:
        ext = e.get("id") or e.get("link") or e.get("title")
        if not ext:
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        ts = datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc) if t else datetime.now(timezone.utc)
        body = ""
        if e.get("content"):
            body = " ".join(c.get("value", "") for c in e["content"])
        body = body or e.get("summary", "")
        items.append(Item(ext_id=str(ext)[:300], ts=ts, title=e.get("title", ""), text=html_to_text(body), link=e.get("link", "")))
    return items


def poll(url: str, etag: str | None, fetch_article: bool = True, timeout: float = 15.0) -> tuple[list[Item], str | None]:
    """Fetch the feed (conditional on etag), keep relevant items, optionally pull the article body for the sentences."""
    headers = {"User-Agent": UA}
    if etag:
        headers["If-None-Match"] = etag
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as h:
        r = h.get(url, headers=headers)
        if r.status_code == 304:
            return [], etag
        r.raise_for_status()
        items = [i for i in parse_feed(r.content) if relevant(i.title + " " + i.text)]
        if fetch_article:
            for i in items[:8]:                       # politeness: at most 8 article fetches per poll
                if i.link and len(i.text) < 600:
                    try:
                        a = h.get(i.link)
                        if a.status_code == 200:
                            i.text = html_to_text(a.text)[:20000]
                    except httpx.HTTPError:
                        pass
        return items, r.headers.get("etag") or etag
