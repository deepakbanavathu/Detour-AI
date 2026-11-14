"""One path from text to zone, used by the console's paste box and by every text poller."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from . import gazetteer, ir as irmod, zones
from .config import CITY_ID


def _dedup_hash(norm: str, handle: str, ts: datetime) -> bytes:
    return hashlib.sha256(f"{norm}|{handle}|{int(ts.timestamp() // 600)}".encode()).digest()


def store_message(c, source_type: str, handle: str, cred: float, ext_id: str, ts: datetime, raw: str, norm: str,
                  city_id: int = CITY_ID) -> tuple[int, bool]:
    """Insert the raw message; returns (msg_id, is_new). A repeat of the same ext_id or the same text is not new."""
    h = _dedup_hash(norm, handle, ts)
    dup = c.execute(
        "SELECT msg_id FROM ingest.raw_message WHERE (source_type = %s AND ext_id = %s) "
        "OR (dedup_hash = %s AND ts_ingested > now() - INTERVAL '24 hours') LIMIT 1",
        (source_type, ext_id, h),
    ).fetchone()
    if dup:
        return int(dup["msg_id"]), False
    row = c.execute(
        """
        INSERT INTO ingest.raw_message (city_id, source_type, source_handle, source_cred, ext_id, ts_published, text_raw, text_norm, dedup_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING msg_id
        """,
        (city_id, source_type, handle, cred, ext_id, ts, raw, norm, h),
    ).fetchone()
    return int(row["msg_id"]), True


def process_sentence(c, msg_id: int | None, raw: str, cred: float, ts: datetime, commit: bool = True,
                     force_status: str | None = None, city_id: int = CITY_ID) -> dict:
    """Normalize, extract the IR, resolve anchors, build and (optionally) commit the zone. Misses go to review."""
    norm = gazetteer.normalize(raw, city_id)
    ir = irmod.extract(raw, norm)
    if ir is None:
        if commit and msg_id:
            c.execute("INSERT INTO ingest.review (msg_id, sentence, reason) VALUES (%s, %s, 'NO_ANCHOR')", (msg_id, norm))
            c.commit()
        return {"ok": False, "reason": "no anchor found by regex or model", "normalized": norm}

    ir_id = None
    if commit and msg_id:
        ir_id = c.execute(
            "INSERT INTO ingest.advisory_ir (msg_id, event_type, cause, ir, model_ver, confidence) VALUES (%s, %s, %s, %s, %s, %s) RETURNING ir_id",
            (msg_id, ir["event_type"], ir["cause"], json.dumps(ir), ir.get("model_ver", "?"), ir["confidence"]),
        ).fetchone()["ir_id"]

    if ir["event_type"] == "LIFTED":
        try:
            a = gazetteer.resolve(c, ir["anchors"][0]["text"], city_id)
        except gazetteer.Unresolved as e:
            return _unresolved(c, msg_id, norm, ir, e, commit)
        lifted = zones.lift_near(c, a, city_id=city_id) if commit else []
        return {"ok": True, "normalized": norm, "ir": ir, "lifted_zones": lifted,
                "anchors": [{"text": a.text, "canonical": a.canonical, "feature_class": a.feature_class, "gaz_id": a.gaz_id, "score": round(a.score, 3)}]}

    try:
        z = zones.resolve_zone(c, ir, ts, cred, city_id)
    except gazetteer.Unresolved as e:
        return _unresolved(c, msg_id, norm, ir, e, commit)

    geom_json = c.execute("SELECT ST_AsGeoJSON(ST_GeomFromEWKT(%s))::json AS g", (z.geom_ewkt,)).fetchone()["g"]
    result = {"status": "DRY_RUN", "zone_id": None, "edges_updated": 0}
    if commit:
        result = zones.commit_zone(c, z, ir_id, city_id, force_status)
    else:
        c.commit()
    return {
        "ok": True, "normalized": norm, "ir": ir,
        "anchors": [{"text": a.text, "canonical": a.canonical, "feature_class": a.feature_class, "gaz_id": a.gaz_id, "score": round(a.score, 3)} for a in z.anchors],
        "zone": {**result, "event_type": z.event_type, "cause": z.cause, "method": z.method,
                 "bearing_center": z.bearing_center, "bearing_halfwidth": z.bearing_halfwidth,
                 "profile_mask": z.profile_mask, "penalty_factor": z.penalty_factor, "confidence": z.confidence,
                 "valid_from": z.valid_from, "valid_to": z.valid_to, "h3_cells": len(z.h3_cover), "geometry": geom_json},
    }


def _unresolved(c, msg_id, norm, ir, e: gazetteer.Unresolved, commit: bool) -> dict:
    if commit and msg_id:
        c.execute(
            "INSERT INTO ingest.review (msg_id, sentence, reason, anchor, candidates, ir) VALUES (%s, %s, %s, %s, %s, %s)",
            (msg_id, norm, "AMBIGUOUS" if e.candidates else "UNRESOLVED", e.text,
             json.dumps([{"gaz_id": x["gaz_id"], "canonical": x["canonical"], "feature_class": x["feature_class"], "score": round(float(x["score"]), 3)} for x in e.candidates]),
             json.dumps(ir)),
        )
    c.commit()
    return {"ok": False, "reason": f"anchor {e.text!r} did not resolve", "candidates": e.candidates, "ir": ir, "normalized": norm}
