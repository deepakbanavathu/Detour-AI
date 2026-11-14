"""WhatsApp Cloud API: webhook verification and parsing, message payloads, sending."""
from __future__ import annotations

import hashlib
import hmac

import httpx

from ..config import WHATSAPP_API_VERSION, WHATSAPP_PHONE_ID, WHATSAPP_TOKEN


def configured() -> bool:
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], digest)


def parse_webhook(payload: dict) -> tuple[list[dict], list[dict]]:
    """-> (inbound messages, delivery statuses). Unknown message types are skipped."""
    msgs: list[dict] = []
    statuses: list[dict] = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value") or {}
            for m in v.get("messages", []):
                base = {"from": m.get("from"), "msg_id": m.get("id")}      # msg_id = provider id; "id" is reserved for a button's reply id
                t = m.get("type")
                if t == "text":
                    msgs.append({**base, "kind": "text", "text": (m.get("text") or {}).get("body", "")})
                elif t == "location":
                    loc = m.get("location") or {}
                    msgs.append({**base, "kind": "location", "lat": float(loc["latitude"]), "lng": float(loc["longitude"])})
                elif t == "interactive":
                    i = m.get("interactive") or {}
                    r = i.get("button_reply") or i.get("list_reply") or {}
                    msgs.append({**base, "kind": "button", "id": r.get("id", ""), "text": r.get("title", "")})
                elif t == "button":                              # quick-reply button on a template
                    b = m.get("button") or {}
                    msgs.append({**base, "kind": "text", "text": b.get("text") or b.get("payload", "")})
            for s in v.get("statuses", []):
                statuses.append({"id": s.get("id"), "status": (s.get("status") or "").upper(), "recipient": s.get("recipient_id"),
                                 "error": "; ".join(e.get("title", "") for e in s.get("errors") or []) or None})
    return msgs, statuses


def _base(to: str) -> dict:
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}


def render(reply: dict, to: str) -> dict:
    """A channel-neutral reply (chat.py) as a Cloud API message body."""
    if reply["type"] == "buttons":
        return {**_base(to), "type": "interactive", "interactive": {
            "type": "button", "body": {"text": reply["text"][:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in reply["buttons"]]}}}
    if reply["type"] == "ask_location":
        return {**_base(to), "type": "interactive", "interactive": {
            "type": "location_request_message", "body": {"text": reply["text"][:1024]}, "action": {"name": "send_location"}}}
    return {**_base(to), "type": "text", "text": {"body": reply["text"][:4096], "preview_url": False}}


def link_message(to: str, body: str, button: str, url: str) -> dict:
    """Free-form message with one URL button; allowed only inside the 24 h window the user opened."""
    return {**_base(to), "type": "interactive", "interactive": {
        "type": "cta_url", "body": {"text": body[:1024]},
        "action": {"name": "cta_url", "parameters": {"display_text": button[:20], "url": url}}}}


def template_message(to: str, name: str, body_params: list[str], url_suffix: str | None, lang: str = "en") -> dict:
    comps: list[dict] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}]
    if url_suffix is not None:
        comps.append({"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": url_suffix}]})
    return {**_base(to), "type": "template", "template": {"name": name, "language": {"code": lang}, "components": comps}}


def send(payload: dict, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """-> (provider message id, error). With no credentials nothing is sent and the error says so."""
    if not configured():
        return None, "WhatsApp is not configured"
    try:
        r = httpx.post(f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_ID}/messages", json=payload,
                       headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=timeout)
        body = r.json()
        if r.status_code >= 400:
            return None, ((body.get("error") or {}).get("message") or f"HTTP {r.status_code}")[:300]
        return (body.get("messages") or [{}])[0].get("id"), None
    except (httpx.HTTPError, ValueError) as e:
        return None, str(e)[:300]
