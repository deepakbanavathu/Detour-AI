"""X API v2, pay-per-use: poll a handle's own posts with since_id. No replies, no reposts, no streaming."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

BASE = "https://api.x.com/2"


@dataclass
class Post:
    ext_id: str
    ts: datetime
    text: str


def parse_timeline(payload: dict) -> tuple[list[Post], str | None]:
    posts = [Post(ext_id=t["id"], ts=datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")), text=t["text"])
             for t in payload.get("data", [])]
    newest = (payload.get("meta") or {}).get("newest_id")
    return posts, newest


def resolve_user_id(handle: str, bearer: str, timeout: float = 15.0) -> str:
    r = httpx.get(f"{BASE}/users/by/username/{handle.lstrip('@')}", headers={"Authorization": f"Bearer {bearer}"}, timeout=timeout)
    r.raise_for_status()
    return r.json()["data"]["id"]


def poll(user_id: str, bearer: str, since_id: str | None, timeout: float = 15.0) -> tuple[list[Post], str | None]:
    params = {"exclude": "replies,retweets", "max_results": "20", "tweet.fields": "created_at,lang"}
    if since_id:
        params["since_id"] = since_id
    r = httpx.get(f"{BASE}/users/{user_id}/tweets", params=params, headers={"Authorization": f"Bearer {bearer}"}, timeout=timeout)
    if r.status_code == 429:
        return [], since_id                          # back off; the worker retries at the next interval
    r.raise_for_status()
    posts, newest = parse_timeline(r.json())
    return posts, newest or since_id
