"""Ingestion worker: polls every due source in ingest.source and runs the advisory pipeline.

Run: python -m app.worker        (the compose 'worker' service does this)
Keys come from the environment: X_BEARER_TOKEN, TOMTOM_KEY. A source whose key is missing is skipped and says so.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from . import pipeline, zones
from . import usage
from .channels import fanout
from .config import CITY_ID
from .db import close_pool, conn, open_pool
from .ingest import openmeteo, rss, tomtom, xapi
from .ingest.text import advisory_sentences, relevant

log = logging.getLogger("detour.worker")
TICK_S = 15
EXPIRE_EVERY_S = 300
SKIPS_EVERY_S = 900


def _bbox(c, city_id: int) -> tuple[float, float, float, float]:
    r = c.execute("SELECT ST_XMin(bbox) a, ST_YMin(bbox) b, ST_XMax(bbox) c, ST_YMax(bbox) d FROM geo.city WHERE city_id = %s", (city_id,)).fetchone()
    return (r["a"], r["b"], r["c"], r["d"])


def _text_items(c, src: dict, source_type: str, items: list[tuple[str, datetime, str]]) -> tuple[int, int]:
    """items: (ext_id, ts, text). Returns (new messages, zones committed)."""
    n_new = n_zone = 0
    for ext_id, ts, text in items:
        sentences = advisory_sentences(text) or ([text] if len(text) <= 400 and relevant(text) else [])
        if not sentences:
            continue
        norm_all = " ".join(sentences)[:4000]
        msg_id, is_new = pipeline.store_message(c, source_type, src["handle"] or src["name"], src["cred"], ext_id, ts, text[:8000], norm_all, src["city_id"])
        c.commit()
        if not is_new:
            continue
        n_new += 1
        for s in sentences:
            try:
                out = pipeline.process_sentence(c, msg_id, s, src["cred"], ts, commit=True, city_id=src["city_id"])
                if out.get("ok") and (out.get("zone") or {}).get("zone_id"):
                    n_zone += 1
            except Exception:                                   # one bad sentence must not stop the feed
                c.rollback()
                log.exception("sentence failed: %s", s[:120])
    return n_new, n_zone


def poll_rss(c, src: dict) -> tuple[int, int, str | None]:
    items, etag = rss.poll(src["handle"], src["cursor"], fetch_article=src["config"].get("fetch_article", True))
    n, z = _text_items(c, src, "RSS", [(i.ext_id, i.ts, f"{i.title}. {i.text}") for i in items])
    return n, z, etag


def poll_x(c, src: dict) -> tuple[int, int, str | None]:
    bearer = os.environ.get("X_BEARER_TOKEN", "")
    if not bearer:
        raise RuntimeError("skipped: X_BEARER_TOKEN is not set")
    uid = src["config"].get("user_id")
    if not uid:
        uid = xapi.resolve_user_id(src["handle"], bearer)
        c.execute("UPDATE ingest.source SET config = config || %s::jsonb WHERE source_id = %s", (json.dumps({"user_id": uid}), src["source_id"]))
        c.commit()
    posts, newest = xapi.poll(uid, bearer, src["cursor"])
    n, z = _text_items(c, src, "X", [(p.ext_id, p.ts, p.text) for p in posts])
    return n, z, newest


def poll_tomtom(c, src: dict) -> tuple[int, int, str | None]:
    key = os.environ.get("TOMTOM_KEY", "")
    if not key:
        raise RuntimeError("skipped: TOMTOM_KEY is not set")
    now = datetime.now(timezone.utc)
    incidents = tomtom.poll(key, _bbox(c, src["city_id"]), src["config"].get("categories", [1, 3, 8, 9, 11]))
    n_new = n_zone = 0
    for i in incidents:
        vt = tomtom.valid_to(i, now, src["interval_s"])
        text = f"{i.description}" + (f" from {i.frm} to {i.to}" if i.frm and i.to else "")
        msg_id, is_new = pipeline.store_message(c, "TOMTOM", "TomTom", src["cred"], i.ext_id, i.start, text, text.lower(), src["city_id"])
        if not is_new:                                          # still present: keep its zone alive
            c.execute(
                "UPDATE geo.zone_of_avoidance z SET valid_to = GREATEST(z.valid_to, %s) FROM ingest.advisory_ir a "
                "WHERE a.ir_id = z.ir_id AND a.msg_id = %s AND z.status IN ('ACTIVE','DECAYING')", (vt, msg_id))
            c.commit()
            continue
        n_new += 1
        ir = {"event_type": i.event_type, "cause": i.cause, "anchors": [{"text": t, "role": r} for t, r in ((i.frm, "FROM"), (i.to, "TO")) if t],
              "vehicle_scope": ["ALL"], "confidence": src["cred"], "model_ver": "tomtom-incidents-5", "incident_id": i.ext_id}
        ir_id = c.execute(
            "INSERT INTO ingest.advisory_ir (msg_id, event_type, cause, ir, model_ver, confidence) VALUES (%s, %s, %s, %s, %s, %s) RETURNING ir_id",
            (msg_id, i.event_type, i.cause, json.dumps(ir), ir["model_ver"], src["cred"])).fetchone()["ir_id"]
        z = zones.zone_from_geometry(c, i.geometry, i.event_type, i.cause, i.penalty, src["cred"], i.start, vt, src["city_id"])
        # closures and flooding commit as active; softer categories wait for a second source
        out = zones.commit_zone(c, z, ir_id, src["city_id"], "ACTIVE" if i.penalty >= 1.0 else None)
        n_zone += 1 if out.get("zone_id") else 0
    return n_new, n_zone, None


def poll_open_meteo(c, src: dict) -> tuple[int, int, str | None]:
    pts = openmeteo.poll(_bbox(c, src["city_id"]), float(src["config"].get("grid_deg", 0.05)))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for p in pts:
        c.execute(
            """
            INSERT INTO hydro.rain_cell (h3_r8, ts, i_mm_h, acc_1h, acc_3h, api_72h, n_gauges)
            VALUES (h3_lat_lng_to_cell(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 8), %s, %s, %s, %s, %s, 0)
            ON CONFLICT (h3_r8, ts) DO UPDATE SET i_mm_h = EXCLUDED.i_mm_h, acc_1h = EXCLUDED.acc_1h, acc_3h = EXCLUDED.acc_3h, api_72h = EXCLUDED.api_72h
            """,
            (p.lng, p.lat, now, p.i_mm_h, p.acc_1h, p.acc_3h, p.api_72h))
    c.commit()
    return len(pts), 0, None


POLLERS = {"RSS": poll_rss, "X": poll_x, "TOMTOM_INCIDENTS": poll_tomtom, "OPEN_METEO": poll_open_meteo}


def run_due(c) -> int:
    due = c.execute(
        "SELECT * FROM ingest.source WHERE enabled AND city_id = %s AND (last_polled IS NULL OR last_polled + make_interval(secs => interval_s) <= now()) ORDER BY last_polled NULLS FIRST",
        (CITY_ID,),
    ).fetchall()
    for src in due:
        try:
            n, z, cursor = POLLERS[src["kind"]](c, src)
            c.execute(
                "UPDATE ingest.source SET last_polled = now(), last_ok = now(), last_error = NULL, cursor = COALESCE(%s, cursor), "
                "items_total = items_total + %s, zones_total = zones_total + %s WHERE source_id = %s", (cursor, n, z, src["source_id"]))
            log.info("%s %s: %d new, %d zones", src["kind"], src["name"], n, z)
        except Exception as e:
            c.rollback()
            c.execute("UPDATE ingest.source SET last_polled = now(), last_error = %s WHERE source_id = %s", (str(e)[:300], src["source_id"]))
            (log.info if str(e).startswith("skipped") else log.warning)("%s %s: %s", src["kind"], src["name"], e)
        c.commit()
    return len(due)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    open_pool()
    last_expire = 0.0
    last_skips = 0.0
    try:
        while True:
            with conn() as c:
                run_due(c)
                if time.monotonic() - last_expire > EXPIRE_EVERY_S:
                    log.info("expiry pass: %s", zones.expire(c))
                    last_expire = time.monotonic()
                if time.monotonic() - last_skips > SKIPS_EVERY_S:
                    try:
                        found = usage.detect_skips(c)           # several riders leaving the route at the same stretch
                        if found:
                            log.info("skipped stretches: %s new corrections", found)
                    except Exception:
                        c.rollback()
                        log.exception("skip detection failed")
                    last_skips = time.monotonic()
                try:
                    sent = fanout.tick(c)                       # new zones out, cleared notices, SMS fallbacks
                    if any(sent.values()):
                        log.info("channel: %s", sent)
                except Exception:
                    c.rollback()
                    log.exception("fan-out failed")
            time.sleep(TICK_S)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
