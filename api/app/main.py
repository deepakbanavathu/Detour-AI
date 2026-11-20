"""DetourAI API v0.1: dimension-aware routing, places, advisory paste -> zone, console map."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import brief as briefmod, cifs, gazetteer, maps, packs, pipeline, profiles, routing, zones
from .channels import chat, fanout, sms, telegram, whatsapp
from .channels import store as chan_store
from .config import (ADMIN_TOKEN, CITY_ID, PUBLIC_BASE_URL, SMS_PROVIDER, TELEGRAM_WEBHOOK_SECRET, WHATSAPP_ALERT_TEMPLATE,
                     WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN)
from .db import close_pool, conn, open_pool

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("detour.api")
OOPS = chat.text("Something went wrong on our side. Please send that again in a minute.")


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


@app.get("/card/{route_id}")
def card_page(route_id: int):
    """The offline route card for one saved team route."""
    return FileResponse(STATIC / "card.html")


@app.get("/sw.js")
def service_worker():
    # served from the root so it can control /card/*; never cached by the browser's HTTP cache
    return Response((STATIC / "sw.js").read_text(), media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    with conn() as c:
        n = c.execute("SELECT count(*) AS n FROM net.edge").fetchone()["n"]
        g = c.execute("SELECT count(*) AS n FROM geo.gazetteer").fetchone()["n"]
        z = c.execute("SELECT count(*) AS n FROM geo.zone_of_avoidance WHERE status IN ('ACTIVE','DECAYING') AND valid_to > now()").fetchone()["n"]
        r = c.execute("SELECT count(*) AS n FROM ingest.review WHERE status = 'OPEN'").fetchone()["n"]
    return {"ok": True, "edges": n, "gazetteer": g, "active_zones": z, "open_reviews": r, "profiles": profiles.codes()}


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
        "waze_url": maps.waze_url(dest),
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
                "maps_url": maps.maps_url(p.code, dest, maps.waypoints(r.edges)), "waze_url": maps.waze_url(dest),
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
    with conn() as c:
        msg_id = None
        if body.commit:
            norm = gazetteer.normalize(body.text)
            ext_id = hashlib.sha256(f"{norm}|{body.source_handle}|{int(ts.timestamp() // 600)}".encode()).hexdigest()[:32]
            msg_id, _ = pipeline.store_message(c, "PASTE", body.source_handle, body.source_cred, ext_id, ts, body.text, norm, CITY_ID)
            c.commit()
        return pipeline.process_sentence(c, msg_id, body.text, body.source_cred, ts, commit=body.commit, force_status=body.force_status)


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


# ---------------------------------------------------------------- sources and review queue

class SourceIn(BaseModel):
    kind: str = Field(pattern="^(RSS|X)$")
    name: str = Field(min_length=2, max_length=80)
    handle: str = Field(min_length=2, max_length=300, description="feed URL for RSS, @handle for X")
    cred: float = Field(0.75, ge=0, le=1)
    interval_s: int = Field(300, ge=60, le=86400)
    fetch_article: bool = True


@app.get("/v1/sources")
def sources():
    with conn() as c:
        rows = c.execute(
            "SELECT source_id, kind, name, handle, cred, interval_s, enabled, last_polled, last_ok, last_error, items_total, zones_total "
            "FROM ingest.source WHERE city_id = %s ORDER BY kind, name", (CITY_ID,)).fetchall()
    for r in rows:
        r["state"] = source_state(r)
    return rows


def source_state(r: dict) -> str:
    """One word for the console: off, waiting for first poll, needs a key, failing, ok."""
    if not r["enabled"]:
        return "off"
    if r["last_polled"] is None:
        return "waiting for first poll"
    if (r["last_error"] or "").startswith("skipped"):
        return "needs a key"
    return "failing" if r["last_error"] else "ok"


@app.post("/v1/sources")
def source_add(body: SourceIn, x_admin_token: Annotated[str | None, Header()] = None):
    _admin(x_admin_token)
    if body.kind == "RSS" and not body.handle.startswith(("http://", "https://")):
        raise HTTPException(400, "an RSS source needs the feed URL")
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO ingest.source (city_id, kind, name, handle, config, cred, interval_s)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (city_id, kind, handle) DO UPDATE SET name = EXCLUDED.name, cred = EXCLUDED.cred,
              interval_s = EXCLUDED.interval_s, enabled = true
            RETURNING source_id
            """,
            (CITY_ID, body.kind, body.name, body.handle.strip(), json.dumps({"fetch_article": body.fetch_article}), body.cred, body.interval_s),
        ).fetchone()
        c.commit()
    return {"source_id": row["source_id"]}


@app.post("/v1/sources/{source_id}/enabled")
def source_enable(source_id: int, enabled: bool, x_admin_token: Annotated[str | None, Header()] = None):
    _admin(x_admin_token)
    with conn() as c:
        n = c.execute("UPDATE ingest.source SET enabled = %s WHERE source_id = %s AND city_id = %s", (enabled, source_id, CITY_ID)).rowcount
        c.commit()
    if not n:
        raise HTTPException(404, "source not found")
    return {"source_id": source_id, "enabled": enabled}


@app.post("/v1/sources/{source_id}/poll-now")
def source_poll_now(source_id: int, x_admin_token: Annotated[str | None, Header()] = None):
    """Make the worker check this source on its next tick (within 15 seconds)."""
    _admin(x_admin_token)
    with conn() as c:
        n = c.execute("UPDATE ingest.source SET last_polled = NULL WHERE source_id = %s AND city_id = %s AND enabled", (source_id, CITY_ID)).rowcount
        c.commit()
    if not n:
        raise HTTPException(404, "source not found or paused")
    return {"source_id": source_id, "queued": True}


@app.get("/v1/review")
def review_list(limit: int = Query(50, ge=1, le=200)):
    """Advisory sentences the pipeline could not place, newest first, with the closest gazetteer matches."""
    with conn() as c:
        return c.execute(
            """
            SELECT r.review_id, r.sentence, r.reason, r.anchor, r.candidates, r.created_at, m.source_type, m.source_handle
            FROM ingest.review r JOIN ingest.raw_message m USING (msg_id)
            WHERE r.status = 'OPEN' AND m.city_id = %s ORDER BY r.created_at DESC LIMIT %s
            """,
            (CITY_ID, limit),
        ).fetchall()


class ReviewIn(BaseModel):
    action: str = Field(pattern="^(alias|dismiss)$")
    gaz_id: int | None = None                     # for action=alias: the place the anchor text means


@app.post("/v1/review/{review_id}")
def review_resolve(review_id: int, body: ReviewIn, x_admin_token: Annotated[str | None, Header()] = None):
    """alias: teach the gazetteer that this anchor text means gaz_id, then re-run the sentence. dismiss: drop it."""
    _admin(x_admin_token)
    with conn() as c:
        r = c.execute(
            "SELECT r.*, m.source_cred, m.ts_published FROM ingest.review r JOIN ingest.raw_message m USING (msg_id) WHERE r.review_id = %s AND r.status = 'OPEN'",
            (review_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "review item not found or already closed")
        if body.action == "dismiss":
            c.execute("UPDATE ingest.review SET status = 'DISMISSED', resolved_by = 'console' WHERE review_id = %s", (review_id,))
            c.commit()
            return {"review_id": review_id, "status": "DISMISSED"}
        if not body.gaz_id or not r["anchor"]:
            raise HTTPException(400, "alias needs gaz_id, and the item must have an anchor")
        c.execute("INSERT INTO geo.gazetteer_alias (alias, gaz_id, weight) VALUES (%s, %s, 1.0) ON CONFLICT (alias, gaz_id) DO UPDATE SET weight = 1.0",
                  (gazetteer.normalize(r["anchor"]), body.gaz_id))
        c.execute("UPDATE ingest.review SET status = 'RESOLVED', resolved_by = 'console' WHERE review_id = %s", (review_id,))
        c.commit()
        out = pipeline.process_sentence(c, r["msg_id"], r["sentence"], float(r["source_cred"]), r["ts_published"], commit=True)
    return {"review_id": review_id, "status": "RESOLVED", "rerun": {"ok": out.get("ok"), "zone": (out.get("zone") or {}).get("zone_id"), "reason": out.get("reason")}}


# ---------------------------------------------------------------- route packs (offline guidance)

@app.get("/v1/packs/{route_id}")
def pack_get(route_id: int, rebuild: bool = False):
    """Up to three pre-computed routes for a saved team route, with turn lists and decision points."""
    try:
        return packs.get(route_id, rebuild)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (ValueError, routing.NoRoute) as e:
        raise HTTPException(409, str(e)) from None


@app.get("/v1/packs/{route_id}/status")
def pack_status(route_id: int):
    """Which of the pack's routes is best right now; stale=true means fetch the pack again."""
    try:
        return packs.status(route_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


# ---------------------------------------------------------------- commuter channel: webhooks, short links, status

@app.get("/webhooks/whatsapp")
def whatsapp_verify(mode: Annotated[str | None, Query(alias="hub.mode")] = None,
                    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
                    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None):
    """Meta's one-time webhook verification handshake."""
    if WHATSAPP_VERIFY_TOKEN and mode == "subscribe" and token and secrets.compare_digest(token, WHATSAPP_VERIFY_TOKEN):
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "verification failed")


def _whatsapp_process(payload: dict) -> None:
    msgs, statuses = whatsapp.parse_webhook(payload)
    with conn() as c:
        for st in statuses:
            if st["id"]:
                fanout.update_status(c, st["id"], st["status"], st["error"])
        for m in msgs:
            if not m.get("from") or not m.get("msg_id"):
                continue
            fresh = c.execute("INSERT INTO client.inbound_seen (provider_msg_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING 1", (m["msg_id"],)).fetchone()
            c.commit()
            if not fresh:
                continue                                   # a redelivery of something already handled
            try:
                replies = chan_store.handle_inbound(c, "WHATSAPP", m["from"], m)
            except Exception:
                c.rollback()
                log.exception("whatsapp inbound failed")
                replies = [OOPS]
            for r in replies:
                whatsapp.send(whatsapp.render(r, m["from"]))


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request):
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256"), WHATSAPP_APP_SECRET):
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "not JSON") from None
    await run_in_threadpool(_whatsapp_process, payload)
    return {"ok": True}


@app.post("/webhooks/telegram")
def telegram_webhook(update: Annotated[dict, Body()], x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None):
    if not TELEGRAM_WEBHOOK_SECRET or not x_telegram_bot_api_secret_token or \
            not secrets.compare_digest(x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(403, "bad secret")
    m = telegram.parse_update(update)
    if m is None or m.get("live"):
        return {"ok": True}                                # live-location edits are for the route card, not onboarding
    with conn() as c:
        try:
            replies = chan_store.handle_inbound(c, "TELEGRAM", m["from"], m)
        except Exception:
            c.rollback()
            log.exception("telegram inbound failed")
            replies = [OOPS]
    for r in replies:
        telegram.send(telegram.render(r, m["from"]))
    return {"ok": True}


@app.get("/r/{token}")
def short_link(token: str):
    """Opens the stored Google Maps link. Only links this server generated can be reached, and only to Google Maps."""
    with conn() as c:
        row = c.execute("SELECT target_url FROM client.short_link WHERE token = %s AND expires_at > now()", (token,)).fetchone()
        if row is None or not row["target_url"].startswith("https://www.google.com/maps/"):
            raise HTTPException(404, "link not found or expired")
        c.execute("UPDATE client.alert_sent SET opened_at = COALESCE(opened_at, now()) WHERE token = %s", (token,))
        c.commit()
    return RedirectResponse(row["target_url"], status_code=302)


@app.get("/v1/channels")
def channels():
    """What is switched on, how many people are subscribed, and how the last day's messages fared."""
    with conn() as c:
        subs = c.execute("SELECT channel, count(*) FILTER (WHERE stopped_at IS NULL) AS active, count(*) FILTER (WHERE stopped_at IS NOT NULL) AS stopped "
                         "FROM client.subscriber GROUP BY channel").fetchall()
        box = c.execute("SELECT channel, status, count(*) AS n FROM client.outbox WHERE created_at > now() - INTERVAL '24 hours' GROUP BY channel, status").fetchall()
        opened = c.execute("SELECT count(*) FILTER (WHERE opened_at IS NOT NULL) AS opened, count(*) AS sent FROM client.alert_sent WHERE sent_at > now() - INTERVAL '24 hours'").fetchone()
    by = {r["channel"]: {"active": r["active"], "stopped": r["stopped"]} for r in subs}
    day: dict[str, dict[str, int]] = {}
    for r in box:
        day.setdefault(r["channel"], {})[r["status"].lower()] = r["n"]
    return {
        "whatsapp": {"configured": whatsapp.configured(), "template": bool(WHATSAPP_ALERT_TEMPLATE and PUBLIC_BASE_URL), "webhook_secret": bool(WHATSAPP_APP_SECRET),
                     **by.get("WHATSAPP", {"active": 0, "stopped": 0}), "last_24h": day.get("WHATSAPP", {})},
        "telegram": {"configured": telegram.configured(), **by.get("TELEGRAM", {"active": 0, "stopped": 0}), "last_24h": day.get("TELEGRAM", {})},
        "sms": {"provider": SMS_PROVIDER or None, "configured": sms.configured(), "last_24h": day.get("SMS", {})},
        "links": {"short_links": bool(PUBLIC_BASE_URL), "alerts_sent": opened["sent"], "opened": opened["opened"]},
    }


# ---------------------------------------------------------------- CIFS feed (Waze for Cities format)

@app.get("/v1/feeds/cifs.json")
def cifs_json():
    """Active, confident zones as closures and incidents. A traffic authority registers this URL in the Waze Partner Hub."""
    with conn() as c:
        incidents, _ = cifs.build(c)
    return {"incidents": incidents}


@app.get("/v1/feeds/cifs.xml")
def cifs_xml():
    with conn() as c:
        incidents, _ = cifs.build(c)
    return Response(cifs.to_xml(incidents), media_type="application/xml")


@app.get("/v1/feeds/cifs/preview")
def cifs_preview():
    """What the feed holds and what was left out, with the reason, for the console and for checking before a hand-over."""
    with conn() as c:
        incidents, skipped = cifs.build(c)
        zones_active = c.execute("SELECT count(*) AS n FROM geo.zone_of_avoidance WHERE city_id = %s AND status = 'ACTIVE' AND valid_to > now()", (CITY_ID,)).fetchone()["n"]
    return {"incidents": len(incidents), "closures": sum(1 for i in incidents if i["type"] == "ROAD_CLOSED"), "active_zones": zones_active,
            "min_confidence": cifs.CIFS_MIN_CONFIDENCE, "skipped": skipped, "sample": incidents[:5]}
