"""Telegram Bot API: update parsing, message payloads, sending. Free, and the bot can only write to people who started it."""
from __future__ import annotations

import httpx

from ..config import TELEGRAM_BOT_TOKEN


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def parse_update(u: dict) -> dict | None:
    """-> {'from': chat id, 'kind': text|button|location, ...} or None for updates we ignore."""
    if "callback_query" in u:
        q = u["callback_query"]
        chat = ((q.get("message") or {}).get("chat") or {}).get("id")
        return {"from": str(chat), "kind": "button", "id": q.get("data", ""), "callback_id": q.get("id")} if chat else None
    m = u.get("message") or u.get("edited_message")
    if not m or "chat" not in m:
        return None
    base = {"from": str(m["chat"]["id"])}
    if "location" in m:
        return {**base, "kind": "location", "lat": float(m["location"]["latitude"]), "lng": float(m["location"]["longitude"]),
                "live": "edited_message" in u or "live_period" in m["location"]}
    if "text" in m:
        t = m["text"].strip()
        return {**base, "kind": "text", "text": "hi" if t.startswith("/start") else t.lstrip("/")}
    return None


def render(reply: dict, chat_id: str) -> dict:
    p: dict = {"chat_id": chat_id, "text": reply["text"][:4096], "disable_web_page_preview": True}
    if reply["type"] == "buttons":
        p["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": i}] for i, t in reply["buttons"]]}
    elif reply["type"] == "ask_location":
        p["reply_markup"] = {"keyboard": [[{"text": "Share a location", "request_location": True}]], "resize_keyboard": True, "one_time_keyboard": True}
    else:
        p["reply_markup"] = {"remove_keyboard": True}
    return p


def link_message(chat_id: str, body: str, button: str, url: str) -> dict:
    return {"chat_id": chat_id, "text": body[:4096], "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[{"text": button, "url": url}]]}}


def send(payload: dict, method: str = "sendMessage", timeout: float = 15.0) -> tuple[str | None, str | None]:
    if not configured():
        return None, "Telegram is not configured"
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", json=payload, timeout=timeout)
        body = r.json()
        if not body.get("ok"):
            return None, str(body.get("description") or f"HTTP {r.status_code}")[:300]
        res = body.get("result")
        return (str(res.get("message_id")) if isinstance(res, dict) else "ok"), None
    except (httpx.HTTPError, ValueError) as e:
        return None, str(e)[:300]
