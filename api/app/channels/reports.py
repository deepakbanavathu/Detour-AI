"""Pinned reports, confirm-or-deny, and reporter trust: the mechanisms that make crowd reports exact.

A subscriber pins a spot; it becomes a small zone on that road. People whose route passes are asked once whether it
is still there. Their answers, weighted by their trust, confirm it, shorten it or remove it. Reporters whose reports
hold up gain trust; reports nobody confirms run out on a short clock.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .. import brief, zones
from ..config import CHECK_AFTER_MIN, CITY_ID, PII_KEY, REPORTS_PER_DAY
from . import alerts, chat, telegram, whatsapp

log = logging.getLogger("detour.reports")

# kind -> (event_type, cause, penalty, minutes it lives with no confirmation)
KIND = {"WATER": ("WATERLOGGING", "WATERLOGGING", 0.6, 120), "CLOSED": ("CLOSURE", "UNKNOWN", 1.0, 180), "ACCIDENT": ("ACCIDENT", "ACCIDENT", 0.5, 45)}
KIND_WORDS = {"WATER": "water on the road", "CLOSED": "a closed road", "ACCIDENT": "an accident"}
SNAP_M, HALF_LEN_M, SAME_PLACE_M = 40.0, 60.0, 30.0
EXTEND_MIN, MAX_LIFE_H = 30, 12
YES_TO_ACTIVATE, NO_TO_END = 0.5, 1.0            # summed trust: about one average person to confirm, two to end
TRUST_MIN, TRUST_MAX = 0.1, 0.95


def start_confidence(trust: float) -> float:
    """A new reporter (0.5) starts a report as a candidate (0.60); a proven one (0.9) starts it active (0.80)."""
    return round(0.35 + 0.5 * max(0.0, min(1.0, trust)), 3)


def adjust_trust(trust: float, outcome: str) -> float:
    step = {"CONFIRMED": 0.10, "REFUTED": -0.15, "EXPIRED": -0.03}.get(outcome, 0.0)
    return round(max(TRUST_MIN, min(TRUST_MAX, trust + step)), 3)


def vote_effect(origin: str, status: str, yes_w: float, no_w: float) -> str:
    """What the summed answers do to a zone: ACTIVATE, KEEP, DECAY (shorten) or REFUTE (remove).
    Crowd reports can be removed by the crowd; zones from an authority or a feed are only shortened."""
    if no_w - yes_w >= NO_TO_END:
        return "REFUTE" if origin == "CROWD" else "DECAY"
    if status in ("CANDIDATE", "DECAYING") and yes_w - no_w >= YES_TO_ACTIVATE:
        return "ACTIVATE"
    return "KEEP"


# ---------------------------------------------------------------- reports

def create(c, sub_id: int, kind: str, lat: float, lng: float, now: datetime | None = None, city_id: int = CITY_ID) -> dict:
    now = now or datetime.now(timezone.utc)
    event_type, cause, penalty, life_min = KIND[kind]
    sub = c.execute("SELECT trust FROM client.subscriber WHERE sub_id = %s", (sub_id,)).fetchone()
    trust = float(sub["trust"]) if sub else 0.5
    today = c.execute("SELECT count(*) AS n FROM client.report WHERE sub_id = %s AND created_at > %s", (sub_id, now - timedelta(hours=20))).fetchone()["n"]
    if today >= REPORTS_PER_DAY:
        return {"ok": False, "reason": "limit"}
    e = c.execute(
        """
        SELECT e.edge_id, e.name, e.length_m, ST_LineLocatePoint(e.geom, p.pt) AS f,
               ST_AsEWKT(ST_LineInterpolatePoint(e.geom, ST_LineLocatePoint(e.geom, p.pt))) AS snapped
        FROM (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS pt) p
        JOIN net.edge e ON e.geom && ST_Expand(p.pt, 0.0006)
        WHERE ST_DWithin(e.geom::geography, p.pt::geography, %s)
        ORDER BY (e.name IS NULL), e.geom <-> p.pt LIMIT 1
        """,
        (lng, lat, SNAP_M)).fetchone()
    if e is None:
        return {"ok": False, "reason": "no_road"}
    road = e["name"] or "the road"

    # the same thing already on the map: this report confirms it instead of drawing a second zone
    same = c.execute(
        """
        SELECT zone_id FROM geo.zone_of_avoidance z
        WHERE z.city_id = %s AND z.cause = %s AND z.status IN ('ACTIVE','DECAYING','CANDIDATE') AND z.valid_to > now()
          AND ST_DWithin(z.geom::geography, ST_GeomFromEWKT(%s)::geography, %s)
        ORDER BY z.zone_id LIMIT 1
        """,
        (city_id, cause, e["snapped"], SAME_PLACE_M)).fetchone()
    if same:
        zid = int(same["zone_id"])
        c.execute("UPDATE geo.zone_of_avoidance SET corroborations = corroborations + 1 WHERE zone_id = %s", (zid,))
        c.execute("INSERT INTO client.report (sub_id, kind, geom, zone_id) VALUES (%s, %s, ST_GeomFromEWKT(%s), %s)", (sub_id, kind, e["snapped"], zid))
        res = vote(c, zid, sub_id, True, now)        # recorded, so this person is not asked about it and a later No replaces it
        return {"ok": True, "merged": True, "zone_id": zid, "road": road, "status": res["status"]}

    half = HALF_LEN_M / max(float(e["length_m"]), 1.0)
    spine = c.execute(
        "SELECT ST_AsGeoJSON(ST_LineSubstring(geom, GREATEST(0, %s - %s), LEAST(1, %s + %s)))::json AS g FROM net.edge WHERE edge_id = %s",
        (e["f"], half, e["f"], half, e["edge_id"])).fetchone()["g"]
    z = zones.zone_from_geometry(c, spine, event_type, cause, penalty, start_confidence(trust), now, now + timedelta(minutes=life_min), city_id, buffer_m=25)
    z.origin, z.method = "CROWD", "crowd_pin"
    text = f"crowd report: {KIND_WORDS[kind]} on {road}"
    msg_id = c.execute(
        "INSERT INTO ingest.raw_message (city_id, source_type, source_handle, source_cred, ext_id, ts_published, text_raw, text_norm, dedup_hash) "
        "VALUES (%s, 'CROWD', 'crowd', %s, %s, %s, %s, %s, sha256(%s::bytea)) RETURNING msg_id",
        (city_id, trust, f"crowd-{sub_id}-{int(now.timestamp())}", now, text, text.lower(), f"{sub_id}-{now.timestamp()}".encode())).fetchone()["msg_id"]
    ir = {"event_type": event_type, "cause": cause, "anchors": [{"text": road.lower(), "role": "AT"}] if e["name"] else [],
          "vehicle_scope": ["ALL"], "confidence": start_confidence(trust), "model_ver": "crowd-pin"}
    ir_id = c.execute("INSERT INTO ingest.advisory_ir (msg_id, event_type, cause, ir, model_ver, confidence) VALUES (%s, %s, %s, %s, 'crowd-pin', %s) RETURNING ir_id",
                      (msg_id, event_type, cause, json.dumps(ir), ir["confidence"])).fetchone()["ir_id"]
    out = zones.commit_zone(c, z, ir_id, city_id)
    c.execute("INSERT INTO client.report (sub_id, kind, geom, zone_id) VALUES (%s, %s, ST_GeomFromEWKT(%s), %s)", (sub_id, kind, e["snapped"], out.get("zone_id")))
    c.commit()
    return {"ok": True, "merged": out["status"] == "MERGED", "zone_id": out.get("zone_id"), "road": road, "status": out["status"]}


def vote(c, zone_id: int, sub_id: int, yes: bool, now: datetime | None = None, record: bool = True) -> dict:
    """One person's answer about one zone, weighted by their trust. A second answer from the same person replaces the first."""
    now = now or datetime.now(timezone.utc)
    z = c.execute("SELECT status, origin, confirm_yes_w, confirm_no_w, valid_from FROM geo.zone_of_avoidance WHERE zone_id = %s", (zone_id,)).fetchone()
    if z is None or z["status"] in ("EXPIRED", "REFUTED"):
        return {"status": z["status"] if z else "GONE", "effect": "KEEP"}
    w = float((c.execute("SELECT trust FROM client.subscriber WHERE sub_id = %s", (sub_id,)).fetchone() or {"trust": 0.5})["trust"])
    yes_w, no_w = float(z["confirm_yes_w"]), float(z["confirm_no_w"])
    if record:
        prev = c.execute("SELECT answer FROM client.zone_check WHERE sub_id = %s AND zone_id = %s", (sub_id, zone_id)).fetchone()
        if prev and prev["answer"] is not None:                 # take the earlier answer back out
            yes_w, no_w = (yes_w - w, no_w) if prev["answer"] else (yes_w, no_w - w)
        c.execute(
            "INSERT INTO client.zone_check (sub_id, zone_id, asked_at, answer, answered_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (sub_id, zone_id) DO UPDATE SET answer = EXCLUDED.answer, answered_at = EXCLUDED.answered_at",
            (sub_id, zone_id, now, yes, now))
    yes_w, no_w = (max(0.0, yes_w) + w, max(0.0, no_w)) if yes else (max(0.0, yes_w), max(0.0, no_w) + w)
    effect = vote_effect(z["origin"], z["status"], yes_w, no_w)
    c.execute("UPDATE geo.zone_of_avoidance SET confirm_yes_w = %s, confirm_no_w = %s WHERE zone_id = %s", (yes_w, no_w, zone_id))
    status = z["status"]
    if yes:
        c.execute("UPDATE geo.zone_of_avoidance SET valid_to = LEAST(valid_from + make_interval(hours => %s), GREATEST(valid_to, %s)), "
                  "confidence = LEAST(0.97, confidence + 0.05) WHERE zone_id = %s", (MAX_LIFE_H, now + timedelta(minutes=EXTEND_MIN), zone_id))
    if effect == "ACTIVATE":
        c.execute("UPDATE geo.zone_of_avoidance SET status = 'ACTIVE', confidence = GREATEST(confidence, 0.75) WHERE zone_id = %s", (zone_id,))
        c.execute("SELECT geo.apply_zone(%s)", (zone_id,))
        status = "ACTIVE"
    elif effect == "DECAY":
        c.execute("UPDATE geo.zone_of_avoidance SET status = 'DECAYING', valid_to = LEAST(valid_to, %s) WHERE zone_id = %s", (now + timedelta(minutes=20), zone_id))
        status = "DECAYING"
    elif effect == "REFUTE":
        c.commit()
        zones.set_status(c, zone_id, "REFUTED", "crowd")
        status = "REFUTED"
    c.commit()
    return {"status": status, "effect": effect, "yes_w": round(yes_w, 2), "no_w": round(no_w, 2)}


def settle(c) -> int:
    """Close out pending reports and move their reporters' trust."""
    rows = c.execute(
        """
        SELECT r.report_id, r.sub_id, s.trust,
               CASE WHEN z.zone_id IS NULL OR z.status = 'REFUTED' THEN 'REFUTED'
                    WHEN z.corroborations >= 2 OR z.confirm_yes_w >= %s THEN 'CONFIRMED'
                    WHEN z.status = 'EXPIRED' OR z.valid_to < now() THEN 'EXPIRED' END AS outcome
        FROM client.report r LEFT JOIN geo.zone_of_avoidance z USING (zone_id) LEFT JOIN client.subscriber s USING (sub_id)
        WHERE r.outcome = 'PENDING'
        """,
        (YES_TO_ACTIVATE,)).fetchall()
    n = 0
    for r in rows:
        if not r["outcome"]:
            continue
        c.execute("UPDATE client.report SET outcome = %s WHERE report_id = %s", (r["outcome"], r["report_id"]))
        if r["sub_id"] is not None:
            c.execute("UPDATE client.subscriber SET trust = %s WHERE sub_id = %s", (adjust_trust(float(r["trust"]), r["outcome"]), r["sub_id"]))
        n += 1
    c.commit()
    return n


# ---------------------------------------------------------------- asking people on the route

def check_question(kind: str, where: str, zone_id: int, crowd: bool) -> dict:
    if crowd:
        return chat.buttons(f"Someone reported {kind} {where}. Are you seeing it?", [(f"zc_y_{zone_id}", "Yes, it is there"), (f"zc_n_{zone_id}", "No, it is clear")])
    return chat.buttons(f"Is it still blocked {where}?", [(f"zc_y_{zone_id}", "Still blocked"), (f"zc_n_{zone_id}", "Clear now")])


def ask(c, now: datetime | None = None) -> int:
    """Send the one question per person per zone: to people alerted a while ago, and to a few people whose route
    passes an unconfirmed crowd report. WhatsApp only inside the 24 hour window; Telegram always."""
    now = now or datetime.now(timezone.utc)
    reachable = "(s.channel = 'TELEGRAM' OR s.last_inbound_at > now() - interval '23 hours 30 minutes')"
    rows = c.execute(
        f"""
        (SELECT a.sub_id, a.zone_id, false AS crowd FROM client.alert_sent a
           JOIN geo.zone_of_avoidance z ON z.zone_id = a.zone_id AND z.status IN ('ACTIVE','DECAYING') AND z.valid_to > now()
           JOIN client.subscriber s ON s.sub_id = a.sub_id AND s.stopped_at IS NULL
          WHERE a.sent_at < now() - make_interval(mins => %(after)s) AND a.sent_at > now() - interval '3 hours' AND {reachable}
            AND NOT EXISTS (SELECT 1 FROM client.zone_check k WHERE k.sub_id = a.sub_id AND k.zone_id = a.zone_id)
          LIMIT 20)
        UNION ALL
        (SELECT DISTINCT ON (z.zone_id, s.sub_id) s.sub_id, z.zone_id, true FROM geo.zone_of_avoidance z
           JOIN client.corridor k ON k.cells_r9 && z.h3_cover_r9
           JOIN client.subscriber s ON s.sub_id = k.sub_id AND s.stopped_at IS NULL
          WHERE z.city_id = %(city)s AND z.origin = 'CROWD' AND z.status = 'CANDIDATE' AND z.valid_to > now() AND {reachable}
            AND NOT EXISTS (SELECT 1 FROM client.report r WHERE r.zone_id = z.zone_id AND r.sub_id = s.sub_id)
            AND NOT EXISTS (SELECT 1 FROM client.zone_check q WHERE q.sub_id = s.sub_id AND q.zone_id = z.zone_id)
            AND (SELECT count(*) FROM client.zone_check q WHERE q.zone_id = z.zone_id) < 3
          LIMIT 6)
        """,
        {"after": CHECK_AFTER_MIN, "city": CITY_ID}).fetchall()
    sent = 0
    for r in rows:
        z = c.execute("SELECT z.cause, (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir FROM geo.zone_of_avoidance z WHERE z.zone_id = %s", (r["zone_id"],)).fetchone()
        s = c.execute("SELECT channel, pgp_sym_decrypt(channel_id_enc, %s) AS chan_id FROM client.subscriber WHERE sub_id = %s", (PII_KEY, r["sub_id"])).fetchone()
        q = check_question(alerts.CAUSE.get(z["cause"], "a restriction").lower(), brief._where(z["ir"], r["zone_id"]), r["zone_id"], r["crowd"])
        c.execute("INSERT INTO client.zone_check (sub_id, zone_id, asked_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (r["sub_id"], r["zone_id"], now))
        c.commit()
        ok, _err = telegram.send(telegram.render(q, s["chan_id"])) if s["channel"] == "TELEGRAM" else whatsapp.send(whatsapp.render(q, s["chan_id"]))
        sent += 1 if ok else 0
    return sent


def tick(c) -> dict:
    return {"asked": ask(c), "settled": settle(c)}
