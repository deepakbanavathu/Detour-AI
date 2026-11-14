"""SMS fallback. Providers differ and Indian routes need DLT-registered templates, so the provider is pluggable.

SMS_PROVIDER='' disables the fallback; 'log' records what would be sent; 'msg91' posts to MSG91's flow endpoint.
The MSG91 adapter follows their documented v5 flow call but has not been exercised against a live account.
"""
from __future__ import annotations

import httpx

from ..config import MSG91_AUTHKEY, MSG91_TEMPLATE_ID, SMS_PROVIDER


def configured() -> bool:
    return SMS_PROVIDER == "log" or (SMS_PROVIDER == "msg91" and bool(MSG91_AUTHKEY and MSG91_TEMPLATE_ID))


def send(to_msisdn: str, text: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """to_msisdn: digits with country code, e.g. 9198xxxxxxxx (WhatsApp's wa_id has this form)."""
    if SMS_PROVIDER == "log":
        return "log", None
    if SMS_PROVIDER == "msg91" and MSG91_AUTHKEY and MSG91_TEMPLATE_ID:
        try:
            r = httpx.post("https://control.msg91.com/api/v5/flow/", timeout=timeout,
                           headers={"authkey": MSG91_AUTHKEY, "content-type": "application/json"},
                           json={"template_id": MSG91_TEMPLATE_ID, "recipients": [{"mobiles": to_msisdn, "var1": text[:300]}]})
            body = r.json()
            if r.status_code >= 400 or body.get("type") == "error":
                return None, str(body.get("message") or f"HTTP {r.status_code}")[:300]
            return str(body.get("message") or "accepted"), None
        except (httpx.HTTPError, ValueError) as e:
            return None, str(e)[:300]
    return None, "SMS is not configured"
