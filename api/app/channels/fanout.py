"""Zone -> messages: who is told, what they are told, the cleared notice, and the SMS fallback."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from .. import brief, maps, profiles, routing
from ..config import (ALERTS_PER_DAY, CITY_ID, PII_KEY, PUBLIC_BASE_URL, SMS_FALLBACK_AFTER_S, WHATSAPP_ALERT_TEMPLATE,
                      WHATSAPP_CLEARED_TEMPLATE)
from . import alerts, sms, telegram, whatsapp

log = logging.getLogger("detour.fanout")
IST = timezone(timedelta(hours=5, minutes=30))
WINDOW_24H = timedelta(hours=23, minutes=30)

_MATCH = """
SELECT DISTINCT ON (s.sub_id) s.sub_id, s.channel, pgp_sym_decrypt(s.channel_id_enc, %(key)s) AS chan_id, s.profile, s.last_inbound_at,
       c.corr_id, c.label, c.token AS trip_token, pgp_sym_decrypt(c.origin_enc, %(key)s) AS o, pgp_sym_decrypt(c.dest_enc, %(key)s) AS d
FROM geo.zone_of_avoidance z
JOIN client.corridor c ON c.cells_r9 && z.h3_cover_r9
JOIN client.subscriber s ON s.sub_id = c.sub_id AND s.stopped_at IS NULL
WHERE z.zone_id = %(zone)s
  AND (z.bearing_center IS NULL OR abs(((c.bearing_deg - z.bearing_center + 540)::int %% 360) - 180) <= z.bearing_halfwidth + 30)
  AND (z.profile_mask & CASE s.profile WHEN '2W' THEN 1 WHEN '4W' THEN 12 ELSE 13 END) > 0
  AND EXISTS (SELECT 1 FROM unnest(c.windows) w WHERE %(mod)s >= lower(w) - 30 AND %(mod)s < upper(w))
  AND %(dow)s = ANY (c.dows)
  AND NOT EXISTS (SELECT 1 FROM client.alert_sent a WHERE a.sub_id = s.sub_id AND a.zone_id = z.zone_id)
  AND (SELECT count(*) FROM client.alert_sent a WHERE a.sub_id = s.sub_id AND a.sent_at > now() - INTERVAL '20 hours') < %(cap)s
ORDER BY s.sub_id, c.corr_id
"""


def _latlng(s: str) -> tuple[float, float]:
    a, b = s.split(",")
    return float(a), float(b)


def _short_link(c, url: str) -> tuple[str, str]:
    """-> (token, link the user taps). Without a public base URL the Maps link is sent as it is."""
    token = secrets.token_urlsafe(8)
    c.execute("INSERT INTO client.short_link (token, target_url) VALUES (%s, %s)", (token, url))
    return token, (f"{PUBLIC_BASE_URL}/r/{token}" if PUBLIC_BASE_URL else url)


def _way_round(profile: str, o: tuple[float, float], d: tuple[float, float]) -> tuple[list[str], int | None, str]:
    code = "2W" if profile == "2W" else "4W_HATCH"
    p = profiles.get(code)
    try:
        cur = routing.route(p, o, d)
    except routing.NoRoute:
        return [], None, maps.maps_url(code, d, [])
    names: dict[str, float] = {}
    for e in cur.edges:
        if e.get("name"):
            names[e["name"]] = names.get(e["name"], 0.0) + float(e["length_m"])
    try:
        extra = max(0, round((cur.duration_s - routing.route(p, o, d, use_zones=False).duration_s) / 60))
    except routing.NoRoute:
        extra = None
    return sorted(names, key=names.get, reverse=True)[:3], extra or None, maps.maps_url(code, d, maps.waypoints(cur.edges))


def _send(channel: str, chan_id: str, body: str, link: str, token: str, params: list[str], inside_window: bool,
          waze: str | None = None, card: str | None = None, card_token: str | None = None) -> tuple[str | None, str | None]:
    """The first button opens DetourAI's own live route when this server has a public address; Google Maps and Waze
    follow as choices. Without a public address the Maps link stays the button, as before."""
    if channel == "TELEGRAM":
        rows = ([[{"text": "Open live route", "url": card}]] if card else []) + [[{"text": "Open in Google Maps", "url": link}]]
        if waze:
            rows.append([{"text": "Open in Waze", "url": waze}])
        return telegram.send({"chat_id": chan_id, "text": body[:4096], "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": rows}})
    if inside_window:
        # WhatsApp allows one button; the other apps ride in the text, and only as short links
        if card:
            text = body + f"\nPrefer another app? Google Maps: {link}" + (f"  Waze: {waze}" if waze else "")
            return whatsapp.send(whatsapp.link_message(chan_id, text, "Open live route", card))
        text = body + (f"\nPrefer Waze? {waze}" if waze and PUBLIC_BASE_URL else "")
        return whatsapp.send(whatsapp.link_message(chan_id, text, "Open in Google Maps", link))
    if WHATSAPP_ALERT_TEMPLATE and PUBLIC_BASE_URL:
        return whatsapp.send(whatsapp.template_message(chan_id, WHATSAPP_ALERT_TEMPLATE, params, card_token or token))
    return None, "outside the 24 hour window and no approved template is configured"


def fan_out_zone(c, zone_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(IST)
    z = c.execute(
        "SELECT z.zone_id, z.cause, z.valid_to, (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir "
        "FROM geo.zone_of_avoidance z WHERE z.zone_id = %s", (zone_id,)).fetchone()
    rows = c.execute(_MATCH, {"key": PII_KEY, "zone": zone_id, "mod": local.hour * 60 + local.minute, "dow": local.isoweekday(), "cap": ALERTS_PER_DAY}).fetchall()
    where, until = brief._where(z["ir"], zone_id), brief._clock(z["valid_to"])
    out = {"zone_id": zone_id, "matched": len(rows), "sent": 0, "not_sent": 0}
    for r in rows:
        via, extra, url = _way_round(r["profile"], _latlng(r["o"]), _latlng(r["d"]))
        token, link = _short_link(c, url)
        trip = alerts.trip_label(r["label"])
        body = alerts.alert_text(z["cause"], where, until, via, trip, extra)
        inside = bool(r["last_inbound_at"]) and now - r["last_inbound_at"] < WINDOW_24H
        waze_token, waze_link = _short_link(c, maps.waze_url(_latlng(r["d"])))
        card_token = card_link = None
        if PUBLIC_BASE_URL and r["trip_token"]:
            card_token, card_link = _short_link(c, f"{PUBLIC_BASE_URL}/go/{r['trip_token']}")
        msg_id, err = _send(r["channel"], r["chan_id"], body, link, token, alerts.template_params(z["cause"], where, until, via, trip), inside,
                            waze_link, card_link, card_token)
        status = "SENT" if msg_id else ("NOT_SENT" if err and ("not configured" in err or "no approved template" in err) else "FAILED")
        due = now + timedelta(seconds=SMS_FALLBACK_AFTER_S) if status == "SENT" and r["channel"] == "WHATSAPP" and sms.configured() else None
        c.execute(
            "INSERT INTO client.outbox (sub_id, channel, kind, zone_id, provider_msg_id, status, detail, sms_text, fallback_due_at) "
            "VALUES (%s, %s, 'ALERT', %s, %s, %s, %s, %s, %s)",
            (r["sub_id"], r["channel"], zone_id, msg_id, status, err, alerts.sms_text(z["cause"], where, via, (card_link or link) if PUBLIC_BASE_URL else None), due))
        if status == "SENT":
            c.execute("INSERT INTO client.alert_sent (sub_id, zone_id, corr_id, channel, provider_msg_id, token, waze_token, card_token) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                      (r["sub_id"], zone_id, r["corr_id"], r["channel"], msg_id, token, waze_token, card_token))
            out["sent"] += 1
        else:
            out["not_sent"] += 1
    c.execute("UPDATE geo.zone_of_avoidance SET fanned_out_at = now() WHERE zone_id = %s", (zone_id,))
    c.commit()
    return out


def send_cleared(c, zone_id: int, now: datetime | None = None) -> int:
    """Tell the people who were alerted that the road is open again. Free-form only inside the 24 h window, else the cleared template."""
    now = now or datetime.now(timezone.utc)
    z = c.execute("SELECT z.cause, (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir FROM geo.zone_of_avoidance z WHERE z.zone_id = %s", (zone_id,)).fetchone()
    where = brief._where(z["ir"], zone_id)
    text = alerts.cleared_text(z["cause"], where)
    rows = c.execute(
        "SELECT s.sub_id, s.channel, pgp_sym_decrypt(s.channel_id_enc, %s) AS chan_id, s.last_inbound_at FROM client.alert_sent a "
        "JOIN client.subscriber s USING (sub_id) WHERE a.zone_id = %s AND s.stopped_at IS NULL", (PII_KEY, zone_id)).fetchall()
    n = 0
    for r in rows:
        if r["channel"] == "TELEGRAM":
            msg_id, err = telegram.send({"chat_id": r["chan_id"], "text": text})
        elif r["last_inbound_at"] and now - r["last_inbound_at"] < WINDOW_24H:
            msg_id, err = whatsapp.send(whatsapp.render({"type": "text", "text": text}, r["chan_id"]))
        elif WHATSAPP_CLEARED_TEMPLATE:
            msg_id, err = whatsapp.send(whatsapp.template_message(r["chan_id"], WHATSAPP_CLEARED_TEMPLATE, [alerts.CAUSE.get(z["cause"], "Restriction"), where], None))
        else:
            msg_id, err = None, "outside the 24 hour window and no cleared template is configured"
        c.execute("INSERT INTO client.outbox (sub_id, channel, kind, zone_id, provider_msg_id, status, detail) VALUES (%s, %s, 'CLEARED', %s, %s, %s, %s)",
                  (r["sub_id"], r["channel"], zone_id, msg_id, "SENT" if msg_id else "NOT_SENT", err))
        n += 1 if msg_id else 0
    c.execute("UPDATE geo.zone_of_avoidance SET cleared_sent_at = now() WHERE zone_id = %s", (zone_id,))
    c.commit()
    return n


def sms_fallbacks(c) -> int:
    """WhatsApp alerts with no delivery receipt after the wait go out again as SMS, worded to stand alone."""
    rows = c.execute(
        """
        SELECT o.out_id, o.sub_id, o.zone_id, o.sms_text, pgp_sym_decrypt(s.channel_id_enc, %s) AS msisdn
        FROM client.outbox o JOIN client.subscriber s USING (sub_id)
        WHERE o.channel = 'WHATSAPP' AND o.kind = 'ALERT' AND o.status = 'SENT' AND o.fallback_due_at <= now() AND s.stopped_at IS NULL
        ORDER BY o.out_id LIMIT 50
        """,
        (PII_KEY,)).fetchall()
    n = 0
    for r in rows:
        msg_id, err = sms.send(r["msisdn"], r["sms_text"] or "DetourAI: a closure affects your route.")
        c.execute("UPDATE client.outbox SET status = 'FALLBACK_SENT', fallback_due_at = NULL WHERE out_id = %s", (r["out_id"],))
        c.execute("INSERT INTO client.outbox (sub_id, channel, kind, zone_id, provider_msg_id, status, detail, sms_text) VALUES (%s, 'SMS', 'SMS_FALLBACK', %s, %s, %s, %s, %s)",
                  (r["sub_id"], r["zone_id"], msg_id, "SENT" if msg_id else "FAILED", err, r["sms_text"]))
        n += 1 if msg_id else 0
    c.commit()
    return n


def update_status(c, provider_msg_id: str, status: str, error: str | None) -> None:
    """Delivery receipt from the WhatsApp webhook. A delivered or read message never falls back to SMS."""
    if status not in ("SENT", "DELIVERED", "READ", "FAILED"):
        return
    c.execute(
        """
        UPDATE client.outbox SET
          status = CASE WHEN status = 'FALLBACK_SENT' THEN status WHEN %(st)s = 'SENT' THEN status ELSE %(st)s END,
          delivered_at = CASE WHEN %(st)s IN ('DELIVERED','READ') THEN COALESCE(delivered_at, now()) ELSE delivered_at END,
          fallback_due_at = CASE WHEN %(st)s IN ('DELIVERED','READ') THEN NULL ELSE fallback_due_at END,
          detail = COALESCE(%(err)s, detail)
        WHERE provider_msg_id = %(id)s
        """,
        {"st": status, "err": error, "id": provider_msg_id})
    c.commit()


def tick(c) -> dict:
    """Called by the worker every loop: new zones out, cleared notices, SMS fallbacks."""
    res = {"zones": 0, "sent": 0, "cleared": 0, "sms": 0, "asked": 0, "settled": 0}
    for z in c.execute("SELECT zone_id FROM geo.zone_of_avoidance WHERE city_id = %s AND status = 'ACTIVE' AND fanned_out_at IS NULL AND valid_to > now() ORDER BY zone_id LIMIT 5", (CITY_ID,)).fetchall():
        r = fan_out_zone(c, z["zone_id"])
        res["zones"] += 1
        res["sent"] += r["sent"]
    for z in c.execute(
            "SELECT z.zone_id FROM geo.zone_of_avoidance z WHERE z.city_id = %s AND z.status IN ('REFUTED','EXPIRED') AND z.cleared_sent_at IS NULL "
            "AND z.fanned_out_at > now() - INTERVAL '24 hours' AND EXISTS (SELECT 1 FROM client.alert_sent a WHERE a.zone_id = z.zone_id) LIMIT 5", (CITY_ID,)).fetchall():
        res["cleared"] += send_cleared(c, z["zone_id"])
    if sms.configured():
        res["sms"] = sms_fallbacks(c)
    from . import reports                                           # reports imports this module's siblings
    res.update(reports.tick(c))
    return res
