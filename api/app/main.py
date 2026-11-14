"""DetourAI API v0.1: dimension-aware routing, places, advisory paste -> zone, console map."""
from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import brief as briefmod, gazetteer, ir as irmod, maps, profiles, routing, zones
from .config import ADMIN_TOKEN, CITY_ID
from .db import close_pool, conn, open_pool

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    open_pool()
    profiles.load()
    yield
    close_pool()


app = FastAPI(title="DetourAI API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _latlng(s: str) -> tuple[float, float]:
    try:
        lat, lng = (float(x) for x in s.split(","))
    except ValueError:
        raise HTTPException(400, "expected 'lat,lng'") from None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "coordinate out of range")
    return lat, lng


def _admin(token: str | None) -> None:
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "admin token required")


def _via(edges: list[dict], n: int = 3) -> list[str]:
    """The road names a driver would recognise: the n named roads carrying the most distance, in route order."""
    total: dict[str, float] = {}
    first: dict[str, int] = {}
    for i, e in enumerate(edges):
        name = e.get("name")
        if name:
            total[name] = total.get(name, 0.0) + float(e["length_m"])
            first.setdefault(name, i)
    top = sorted(total, key=lambda k: total[k], reverse=True)[:n]
    return sorted(top, key=lambda k: first[k])


def _snap_dict(s: routing.Snap) -> dict:
    return {"node_id": s.node_id, "edge_id": s.edge_id, "lat": s.lat, "lng": s.lng, "dist_m": round(s.dist_m, 1)}


# ---------------------------------------------------------------- console and health

@app.get("/")
@app.get("/console")
def console():
    return FileResponse(STATIC / "console.html")


@app.get("/brief")
def brief_page():
    return FileResponse(STATIC / "brief.html")


@app.get("/healthz")
def healthz():
    with conn() as c:
        n = c.execute("SELECT count(*) AS n FROM net.edge").fetchone()["n"]
        g = c.execute("SELECT count(*) AS n FROM geo.gazetteer").fetchone()["n"]
        z = c.execute("SELECT count(*) AS n FROM geo.zone_of_avoidance WHERE status IN ('ACTIVE','DECAYING') AND valid_to > now()").fetchone()["n"]
    return {"ok": True, "edges": n, "gazetteer": g, "active_zones": z, "profiles": profiles.codes()}


# ---------------------------------------------------------------- routing

@app.get("/v1/route")
def get_route(
    profile: str = Query("4W_HATCH", description="2W, 3W, 4W_HATCH, 4W_SUV, LCV, HCV, BUS"),
    from_: Annotated[str, Query(alias="from", description="lat,lng")] = ...,
    to: Annotated[str, Query(description="lat,lng")] = ...,
    include_origin: bool = Query(False, description="put the origin in the Maps URL (fleets); commuters use phone position"),
):
    try:
        p = profiles.get(profile)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    origin, dest = _latlng(from_), _latlng(to)
    try:
        r = routing.route(p, origin, dest)
    except routing.NoRoute as e:
        raise HTTPException(404, str(e)) from None
    pins = maps.waypoints(r.edges)
    return {
        "profile": r.profile,
        "distance_m": round(r.distance_m, 1),
        "duration_s": round(r.duration_s, 1),
        "snap_from": _snap_dict(r.snap_from),
        "snap_to": _snap_dict(r.snap_to),
        "bbox_expansions": r.bbox_expansions,
        "via": _via(r.edges),
        "maps_url": maps.maps_url(p.code, dest, pins, origin if include_origin else None),
        "waypoints": [{"lat": a, "lng": b} for a, b in pins],
        "geometry": r.geometry,
        "edges": r.edges,
    }


@app.get("/v1/route/compare")
def compare(
    from_: Annotated[str, Query(alias="from")] = ...,
    to: Annotated[str, Query()] = ...,
    profiles_: Annotated[str, Query(alias="profiles")] = "2W,4W_HATCH",
):
    """The demo moment: the same two points for several profiles, side by side."""
    origin, dest = _latlng(from_), _latlng(to)
    out: dict[str, dict] = {}
    for code in profiles_.split(","):
        try:
            p = profiles.get(code)
            r = routing.route(p, origin, dest)
            out[p.code] = {
                "distance_m": round(r.distance_m, 1),
                "duration_s": round(r.duration_s, 1),
                "edge_ids": [e["edge_id"] for e in r.edges],
                "via": _via(r.edges),
                "maps_url": maps.maps_url(p.code, dest, maps.waypoints(r.edges)),
                "geometry": r.geometry,
                "snap_from": _snap_dict(r.snap_from),
                "snap_to": _snap_dict(r.snap_to),
            }
        except (ValueError, routing.NoRoute) as e:
            out[code.upper()] = {"error": str(e)}
    sets = {k: set(v["edge_ids"]) for k, v in out.items() if "edge_ids" in v}
    if len(sets) >= 2:
        for k, s in sets.items():
            others = set().union(*(o for kk, o in sets.items() if kk != k))
            out[k]["edges_only_this_profile"] = sorted(s - others)
    return out


@app.get("/v1/edges/near")
def edges_near(at: Annotated[str, Query(description="lat,lng")] = ..., radius_m: int = Query(60, ge=5, le=500)):
    """Edges near a point with their dimensions and dynamic state (used to pick the demo gate)."""
    lat, lng = _latlng(at)
    with conn() as c:
        rows = c.execute(
            """
            SELECT e.edge_id, e.name, e.highway, e.oneway, e.length_m, d.width_m, d.width_src, d.clearance_m,
                   d.filterable, d.alley, d.layout_private, d.static_mask,
                   (SELECT string_agg(p.code, ',' ORDER BY p.profile_id) FROM net.vehicle_profile p
                     WHERE (d.static_mask & (1 << p.profile_id)) > 0) AS open_to,
                   y.gate_state, y.two_wheeler_gap, y.zone_penalty_fwd, y.zone_penalty_rev, y.zone_mask,
                   ST_Distance(e.geom::geography, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography) AS dist_m,
                   ST_AsGeoJSON(e.geom)::json AS geometry
            FROM net.edge e JOIN net.edge_dim d USING (edge_id) JOIN net.edge_dynamic y USING (edge_id)
            WHERE ST_DWithin(e.geom::geography, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, %(r)s)
            ORDER BY dist_m LIMIT 20
            """,
            {"lat": lat, "lng": lng, "r": radius_m},
        ).fetchall()
    return rows


# ---------------------------------------------------------------- places

@app.get("/v1/places/resolve")
def places_resolve(q: str = Query(..., min_length=2, max_length=80), near: str | None = Query(None, description="lat,lng")):
    near_wkt = None
    if near:
        lat, lng = _latlng(near)
        near_wkt = f"SRID=4326;POINT({lng} {lat})"
    with conn() as c:
        try:
            a = gazetteer.resolve(c, q, CITY_ID, near_wkt)
            resolved = {"gaz_id": a.gaz_id, "canonical": a.canonical, "feature_class": a.feature_class,
                        "score": round(a.score, 3), "runner_up": round(a.runner_up, 3)}
        except gazetteer.Unresolved:
            resolved = None
        cands = gazetteer.candidates(c, q, CITY_ID)
        ids = [x["gaz_id"] for x in cands]
        geoms = {}
        if ids:
            for r in c.execute("SELECT gaz_id, ST_AsGeoJSON(ST_Centroid(geom))::json AS g FROM geo.gazetteer WHERE gaz_id = ANY(%s)", (ids,)).fetchall():
                geoms[r["gaz_id"]] = r["g"]
    return {"query": q, "normalized": gazetteer.normalize(q), "resolved": resolved,
            "candidates": [{"gaz_id": x["gaz_id"], "canonical": x["canonical"], "feature_class": x["feature_class"],
                            "score": round(float(x["score"]), 3), "centroid": geoms.get(x["gaz_id"])} for x in cands]}


# ---------------------------------------------------------------- advisories -> zones

class PasteIn(BaseModel):
    text: str = Field(min_length=8, max_length=2000)
    source_handle: str = Field("@paste", max_length=60)
    source_cred: float = Field(0.8, ge=0, le=1)
    ts_published: datetime | None = None
    commit: bool = Field(True, description="insert the zone and project it onto edges; false = dry run")
    force_status: str | None = Field(None, pattern="^(ACTIVE|CANDIDATE)$")


@app.post("/v1/advisories/paste")
def advisory_paste(body: PasteIn):
    """Demo moment one: a pasted post becomes a polygon. Returns IR, anchors, zone geometry and the edges affected."""
    ts = body.ts_published or datetime.now(timezone.utc)
    norm = gazetteer.normalize(body.text)
    ir = irmod.extract(body.text, norm)
    if ir is None:
        return {"ok": False, "reason": "no anchor found by regex or model", "normalized": norm}

    with conn() as c:
        msg_id = ir_id = None
        if body.commit:
            dedup = hashlib.sha256(f"{norm}|{body.source_handle}|{int(ts.timestamp() // 600)}".encode()).digest()
            msg_id = c.execute(
                """
                INSERT INTO ingest.raw_message (city_id, source_type, source_handle, source_cred, ext_id, ts_published, text_raw, text_norm, dedup_hash)
                VALUES (%s, 'PASTE', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_type, ext_id) DO UPDATE SET ts_ingested = now()
                RETURNING msg_id
                """,
                (CITY_ID, body.source_handle, body.source_cred, dedup.hex()[:32], ts, body.text, norm, dedup),
            ).fetchone()["msg_id"]
            ir_id = c.execute(
                "INSERT INTO ingest.advisory_ir (msg_id, event_type, cause, ir, model_ver, confidence) VALUES (%s, %s, %s, %s, %s, %s) RETURNING ir_id",
                (msg_id, ir["event_type"], ir["cause"], json.dumps(ir), ir.get("model_ver", "?"), ir["confidence"]),
            ).fetchone()["ir_id"]
        try:
            z = zones.resolve_zone(c, ir, ts, body.source_cred, CITY_ID)
        except gazetteer.Unresolved as e:
            c.commit()
            return {"ok": False, "reason": f"anchor {e.text!r} did not resolve", "candidates": e.candidates, "ir": ir, "normalized": norm}

        geom_json = c.execute("SELECT ST_AsGeoJSON(ST_GeomFromEWKT(%s))::json AS g", (z.geom_ewkt,)).fetchone()["g"]
        result = {"status": "DRY_RUN", "zone_id": None, "edges_updated": 0}
        if body.commit:
            result = zones.commit_zone(c, z, ir_id, CITY_ID, body.force_status)
        else:
            c.commit()

    return {
        "ok": True,
        "normalized": norm,
        "ir": ir,
        "anchors": [{"text": a.text, "canonical": a.canonical, "feature_class": a.feature_class, "gaz_id": a.gaz_id, "score": round(a.score, 3)} for a in z.anchors],
        "zone": {
            **result, "event_type": z.event_type, "cause": z.cause, "method": z.method,
            "bearing_center": z.bearing_center, "bearing_halfwidth": z.bearing_halfwidth,
            "profile_mask": z.profile_mask, "penalty_factor": z.penalty_factor, "confidence": z.confidence,
            "valid_from": z.valid_from, "valid_to": z.valid_to, "h3_cells": len(z.h3_cover), "geometry": geom_json,
        },
    }


@app.get("/v1/zones")
def get_zones(bbox: Annotated[str, Query(description="min_lng,min_lat,max_lng,max_lat")] = ..., include_candidates: bool = False):
    try:
        minx, miny, maxx, maxy = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be min_lng,min_lat,max_lng,max_lat") from None
    if (maxx - minx) * (maxy - miny) > 0.25:
        raise HTTPException(400, "bbox too large")
    statuses = ("ACTIVE", "DECAYING", "CANDIDATE") if include_candidates else ("ACTIVE", "DECAYING")
    with conn() as c:
        rows = c.execute(
            """
            SELECT zone_id, event_type, cause, status, penalty_factor, confidence, bearing_center, bearing_halfwidth,
                   corroborations, valid_from, valid_to, ST_AsGeoJSON(geom)::json AS geometry,
                   (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir
            FROM geo.zone_of_avoidance z
            WHERE city_id = %s AND status = ANY(%s) AND valid_to > now()
              AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            ORDER BY zone_id DESC
            """,
            (CITY_ID, list(statuses), minx, miny, maxx, maxy),
        ).fetchall()
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": r.pop("geometry"), "properties": r} for r in rows]}


class ZoneStatusIn(BaseModel):
    status: str = Field(pattern="^(ACTIVE|CANDIDATE|REFUTED|EXPIRED)$")
    reviewed_by: str | None = Field(None, max_length=60)


@app.post("/v1/zones/{zone_id}/status")
def zone_status(zone_id: int, body: ZoneStatusIn, x_admin_token: Annotated[str | None, Header()] = None):
    _admin(x_admin_token)
    with conn() as c:
        if c.execute("SELECT 1 FROM geo.zone_of_avoidance WHERE zone_id = %s", (zone_id,)).fetchone() is None:
            raise HTTPException(404, "zone not found")
        n = zones.set_status(c, zone_id, body.status, body.reviewed_by)
    return {"zone_id": zone_id, "status": body.status, "edges_reprojected": n}


@app.post("/v1/admin/zones/expire")
def zones_expire(x_admin_token: Annotated[str | None, Header()] = None):
    """Run the 1.7 decay/expiry pass (cron this every 5 minutes)."""
    _admin(x_admin_token)
    with conn() as c:
        return zones.expire(c)


# ---------------------------------------------------------------- admin overlay

class EdgeStatePatch(BaseModel):
    edge_ids: list[int] = Field(min_length=1, max_length=5000)
    gate_state: str | None = Field(None, pattern="^(OPEN|CLOSED_REPORTED|CLOSED_INFERRED|UNKNOWN)$")
    two_wheeler_gap: bool | None = None
    zone_penalty: float | None = Field(None, ge=0, le=1, description="applied to both directions")
    profile_mask: int | None = Field(None, ge=0, le=127)
    speed_now_kmh: float | None = Field(None, ge=0, le=120)
    depth_est_m: float | None = Field(None, ge=0, le=3)


@app.post("/v1/admin/edges/state")
def admin_edge_state(body: EdgeStatePatch, x_admin_token: Annotated[str | None, Header()] = None):
    """Demo and field-override helper: set overlay state on edges directly (Module 5.2 does this autonomously)."""
    _admin(x_admin_token)
    sets: list[str] = []
    params: dict = {"ids": body.edge_ids}
    if body.gate_state is not None:
        sets.append("gate_state = %(gate_state)s")
        params["gate_state"] = body.gate_state
    if body.two_wheeler_gap is not None:
        sets.append("two_wheeler_gap = %(gap)s")
        params["gap"] = body.two_wheeler_gap
    if body.zone_penalty is not None:
        sets.append("zone_penalty_fwd = %(zp)s, zone_penalty_rev = %(zp)s, zone_mask = %(mask)s")
        params["zp"] = body.zone_penalty
        params["mask"] = body.profile_mask if body.profile_mask is not None else 127
    if body.speed_now_kmh is not None:
        sets.append("speed_now_kmh = %(spd)s")
        params["spd"] = body.speed_now_kmh
    if body.depth_est_m is not None:
        sets.append("depth_est_m = %(depth)s")
        params["depth"] = body.depth_est_m
    if not sets:
        raise HTTPException(400, "nothing to set")
    sets.append("updated_at = now()")
    with conn() as c:
        n = c.execute(f"UPDATE net.edge_dynamic SET {', '.join(sets)} WHERE edge_id = ANY(%(ids)s)", params).rowcount
        c.commit()
    return {"updated": n}


# ---------------------------------------------------------------- road brief and team routes

@app.get("/v1/brief")
def get_brief(format: str = Query("json", pattern="^(json|text|email)$")):
    """Today's zones and the team's saved routes. text = WhatsApp-ready, email = email-safe HTML."""
    b = briefmod.build(CITY_ID)
    if format == "text":
        return PlainTextResponse(briefmod.as_text(b))
    if format == "email":
        return HTMLResponse(briefmod.as_email_html(b))
    return b


class TeamRouteIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    from_: str = Field(alias="from", description="lat,lng")
    to: str = Field(description="lat,lng")
    from_label: str | None = Field(None, max_length=80)
    to_label: str | None = Field(None, max_length=80)
    profile: str = "4W_HATCH"


@app.get("/v1/team-routes")
def team_routes():
    with conn() as c:
        return c.execute(
            """
            SELECT route_id, name, profile, origin_label, dest_label,
                   ST_Y(origin) AS from_lat, ST_X(origin) AS from_lng, ST_Y(dest) AS to_lat, ST_X(dest) AS to_lng
            FROM client.team_route WHERE city_id = %s AND active ORDER BY name, profile
            """,
            (CITY_ID,),
        ).fetchall()


@app.post("/v1/team-routes")
def team_route_add(body: TeamRouteIn, x_admin_token: Annotated[str | None, Header()] = None):
    _admin(x_admin_token)
    try:
        p = profiles.get(body.profile)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    (olat, olng), (dlat, dlng) = _latlng(body.from_), _latlng(body.to)
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO client.team_route (city_id, name, origin, dest, origin_label, dest_label, profile)
            VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)
            ON CONFLICT (city_id, name, profile) DO UPDATE
              SET origin = EXCLUDED.origin, dest = EXCLUDED.dest, origin_label = EXCLUDED.origin_label,
                  dest_label = EXCLUDED.dest_label, active = true
            RETURNING route_id
            """,
            (CITY_ID, body.name.strip(), olng, olat, dlng, dlat, body.from_label, body.to_label, p.code),
        ).fetchone()
        c.commit()
    return {"route_id": row["route_id"], "name": body.name.strip(), "profile": p.code}


@app.delete("/v1/team-routes/{route_id}")
def team_route_delete(route_id: int, x_admin_token: Annotated[str | None, Header()] = None):
    _admin(x_admin_token)
    with conn() as c:
        n = c.execute("UPDATE client.team_route SET active = false WHERE route_id = %s AND city_id = %s", (route_id, CITY_ID)).rowcount
        c.commit()
    if not n:
        raise HTTPException(404, "route not found")
    return {"removed": route_id}
