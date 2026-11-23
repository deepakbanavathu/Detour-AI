"""DetourAI API v0.1: dimension-aware routing, places, advisory paste -> zone, console map."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, brief as briefmod, cifs, feedback, gazetteer, geocode, maps, packs, pipeline, profiles, routing, usage, zones
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

# What may be reached through the public address (PUBLIC_BASE_URL). Everything else, including the console, the brief
# and every admin endpoint, answers 404 there and stays reachable only on the local address.
PUBLIC_PREFIXES = ("/webhooks/", "/r/", "/card/", "/v1/packs/", "/go/", "/v1/trips/")
PUBLIC_PATHS = ("/sw.js", "/app-icon.svg", "/v1/feeds/cifs.json", "/v1/feeds/cifs.xml")
_PUBLIC_HOST = PUBLIC_BASE_URL.split("://", 1)[-1].split("/", 1)[0].lower()


def public_path_allowed(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


OPEN_PATHS = ("/login", "/v1/login", "/v1/logout", "/v1/session", "/healthz")
PAGE_PATHS = ("/", "/console", "/brief", "/waze")


@app.middleware("http")
async def public_surface(request: Request, call_next):
    path = request.url.path
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip().lower()
    if _PUBLIC_HOST and host == _PUBLIC_HOST and not (public_path_allowed(path) and request.method in ("GET", "POST", "HEAD")):
        return PlainTextResponse("Not found", status_code=404)

    # everything that is not public by design needs a signed-in session or the admin token
    if auth.REQUIRE_LOGIN and not public_path_allowed(path) and path not in OPEN_PATHS:
        token = request.headers.get("x-admin-token")
        signed_in = auth.valid(request.cookies.get(auth.COOKIE))
        if not signed_in and not (token and secrets.compare_digest(token, ADMIN_TOKEN)):
            if path in PAGE_PATHS and request.method == "GET":
                return RedirectResponse(f"/login?next={path}", status_code=303)
            return Response('{"detail":"sign in required"}', status_code=401, media_type="application/json")
        if signed_in and not token:                 # a session carries admin rights: present it as the token downstream
            request.scope["headers"] = [*request.scope["headers"], (b"x-admin-token", ADMIN_TOKEN.encode())]
    return await call_next(request)


app.add_middleware(GZipMiddleware, minimum_size=1024)      # route geometry and the brief compress about five-fold


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/v1/session")
def session(request: Request):
    return {"signed_in": auth.valid(request.cookies.get(auth.COOKIE)), "login_required": auth.REQUIRE_LOGIN,
            "default_password": auth.uses_default_password()}


@app.post("/v1/login")
def login(body: LoginIn, request: Request):
    who = request.client.host if request.client else "?"
    if auth.locked_out(who):
        raise HTTPException(429, "too many attempts; wait five minutes")
    if not auth.password_ok(body.password):
        auth.record_failure(who)
        raise HTTPException(401, "wrong password")
    auth.clear_failures(who)
    res = Response('{"signed_in":true}', media_type="application/json")
    res.set_cookie(auth.COOKIE, auth.issue(), max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="strict",
                   secure=request.url.scheme == "https", path="/")
    return res


@app.post("/v1/logout")
def logout():
    res = Response('{"signed_in":false}', media_type="application/json")
    res.delete_cookie(auth.COOKIE, path="/")
    return res


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


_COUNTS: dict = {"at": 0.0, "edges": 0, "gazetteer": 0}


@app.get("/healthz")
def healthz():
    with conn() as c:
        if time.monotonic() - _COUNTS["at"] > 300 or not _COUNTS["edges"]:      # the two big tables change only on import
            _COUNTS.update(at=time.monotonic(),
                           edges=c.execute("SELECT count(*) AS n FROM net.edge").fetchone()["n"],
                           gazetteer=c.execute("SELECT count(*) AS n FROM geo.gazetteer").fetchone()["n"])
        n, g = _COUNTS["edges"], _COUNTS["gazetteer"]
        z = c.execute("SELECT count(*) AS n FROM geo.zone_of_avoidance WHERE status IN ('ACTIVE','DECAYING') AND valid_to > now()").fetchone()["n"]
        r = c.execute("SELECT count(*) AS n FROM ingest.review WHERE status = 'OPEN'").fetchone()["n"]
    return {"ok": True, "edges": n, "gazetteer": g, "active_zones": z, "open_reviews": r, "profiles": profiles.codes(),
            "route_cache": dict(routing.CACHE_STATS)}


# ---------------------------------------------------------------- routing

@app.get("/v1/route")
def get_route(
    profile: str = Query("4W_HATCH", description="2W, 3W, 4W_HATCH, 4W_SUV, LCV, HCV, BUS"),
    from_: Annotated[str, Query(alias="from", description="lat,lng")] = ...,
    to: Annotated[str, Query(description="lat,lng")] = ...,
    include_origin: bool = Query(False, description="put the origin in the Maps URL (fleets); commuters use phone position"),
    fresh: bool = Query(False, description="compute even if the answer is remembered (used to time the real search)"),
):
    try:
        p = profiles.get(profile)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    origin, dest = _latlng(from_), _latlng(to)
    try:
        r = routing.route(p, origin, dest, fresh=fresh)
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
    codes = [c for c in profiles_.split(",") if c.strip()][:7]

    def one(code: str):
        try:
            p = profiles.get(code)
            return p.code, routing.route(p, origin, dest), p
        except (ValueError, routing.NoRoute) as e:
            return code.upper(), e, None

    with ThreadPoolExecutor(max_workers=min(4, len(codes) or 1)) as pool:     # each vehicle on its own connection
        results = list(pool.map(one, codes))
    out: dict[str, dict] = {}
    for code, r, p in results:
        if p is None:
            out[code] = {"error": str(r)}
            continue
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


_CITY: dict = {}


def _city() -> dict:
    """The city's name, centre and bounding box, read once."""
    if not _CITY:
        with conn() as c:
            r = c.execute("SELECT name, ST_XMin(bbox) x0, ST_YMin(bbox) y0, ST_XMax(bbox) x1, ST_YMax(bbox) y1 FROM geo.city WHERE city_id = %s", (CITY_ID,)).fetchone()
        _CITY.update(name=r["name"], bbox=(r["x0"], r["y0"], r["x1"], r["y1"]), center={"lat": (r["y0"] + r["y1"]) / 2, "lng": (r["x0"] + r["x1"]) / 2})
    return _CITY


@app.get("/v1/places/suggest")
def places_suggest(q: str = Query(..., min_length=2, max_length=80)):
    """Suggestions while a name is being typed: the city's own gazetteer first, then an OpenStreetMap search for the rest."""
    with conn() as c:
        rows = gazetteer.suggest(c, q, CITY_ID)
    out = [{"gaz_id": r["gaz_id"], "name": r["canonical"], "kind": r["feature_class"].lower(), "lat": r["clat"], "lng": r["clng"],
            "source": "gazetteer", "near": None} for r in rows]
    if len(out) < 3:
        have = {o["name"].lower() for o in out}
        out += [x for x in geocode.search(q, _city()["bbox"]) if x["name"].lower() not in have][: 8 - len(out)]
    return {"query": q, "suggestions": out}


# ---------------------------------------------------------------- advisories -> zones

class PasteIn(BaseModel):
    text: str = Field(min_length=8, max_length=2000)
    source_handle: str = Field("@paste", max_length=60)
    source_cred: float = Field(0.8, ge=0, le=1)
    ts_published: datetime | None = None
    commit: bool = Field(False, description="save the zone and project it onto edges (admin token required); false = dry run")
    force_status: str | None = Field(None, pattern="^(ACTIVE|CANDIDATE)$")


@app.post("/v1/advisories/paste")
def advisory_paste(body: PasteIn, x_admin_token: Annotated[str | None, Header()] = None):
    """A pasted post becomes a polygon. Without the admin token this is always a dry run: nothing is saved, routed
    around, sent to subscribers or published. Saving a zone is an admin action."""
    if body.commit or body.force_status:
        _admin(x_admin_token)
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
            SELECT zone_id, event_type, cause, status, origin, penalty_factor, confidence, bearing_center, bearing_halfwidth,
                   corroborations, valid_from, valid_to, created_at, ST_AsGeoJSON(geom)::json AS geometry,
                   ST_Y(ST_PointOnSurface(geom)) AS lat, ST_X(ST_PointOnSurface(geom)) AS lng,
                   (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir
            FROM geo.zone_of_avoidance z
            WHERE city_id = %s AND status = ANY(%s) AND valid_to > now()
              AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            ORDER BY zone_id DESC
            """,
            (CITY_ID, list(statuses), minx, miny, maxx, maxy),
        ).fetchall()
    for r in rows:
        r["where"] = briefmod._where(r["ir"], r["zone_id"])
        r["until"] = briefmod._clock(r["valid_to"])
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": r.pop("geometry"), "properties": r} for r in rows]}


@app.get("/v1/zones/active")
def zones_active():
    """Every active zone in the city as a short list: what, where, since when, until when, and a point to look at."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT z.zone_id, z.cause, z.status, z.origin, z.created_at, z.valid_to,
                   ST_Y(ST_PointOnSurface(z.geom)) AS lat, ST_X(ST_PointOnSurface(z.geom)) AS lng,
                   (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir
            FROM geo.zone_of_avoidance z
            WHERE z.city_id = %s AND z.status IN ('ACTIVE','DECAYING') AND z.valid_to > now()
            ORDER BY z.created_at DESC LIMIT 200
            """, (CITY_ID,)).fetchall()
    city = _city()
    return {"city": city["name"], "center": city["center"],
            "zones": [{"zone_id": r["zone_id"], "cause": r["cause"], "status": r["status"], "origin": r["origin"], "created_at": r["created_at"],
                       "valid_to": r["valid_to"], "until": briefmod._clock(r["valid_to"]), "where": briefmod._where(r["ir"], r["zone_id"]),
                       "lat": r["lat"], "lng": r["lng"]} for r in rows]}


@app.get("/v1/reports")
def reports_recent(hours: int = Query(6, ge=1, le=48)):
    """Recent crowd reports for the map: what, where, and whether anyone has confirmed it. No reporter details."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT r.report_id, r.kind, r.outcome, r.created_at, r.zone_id, ST_Y(r.geom) AS lat, ST_X(r.geom) AS lng, z.status AS zone_status
            FROM client.report r LEFT JOIN geo.zone_of_avoidance z USING (zone_id)
            WHERE r.created_at > now() - make_interval(hours => %s) AND r.outcome IN ('PENDING','CONFIRMED')
              AND (z.zone_id IS NULL OR (z.status IN ('ACTIVE','DECAYING','CANDIDATE') AND z.valid_to > now()))
            ORDER BY r.created_at DESC LIMIT 300
            """, (hours,)).fetchall()
    return {"reports": rows}


@app.get("/waze")
def waze_page():
    """Waze's own Live Map beside the list of DetourAI zones, for timing who showed a closure first."""
    return FileResponse(STATIC / "waze.html")


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
    """Opens the stored navigation link. Only links this server generated can be reached, and only to Google Maps
    directions or Waze's navigation address; the open is recorded against the alert."""
    with conn() as c:
        row = c.execute("SELECT target_url FROM client.short_link WHERE token = %s AND expires_at > now()", (token,)).fetchone()
        kind = maps.redirect_kind(row["target_url"], PUBLIC_BASE_URL) if row else None
        if kind is None:
            raise HTTPException(404, "link not found or expired")
        c.execute("UPDATE client.alert_sent SET opened_at = COALESCE(opened_at, now()), opened_in = COALESCE(opened_in, %s) "
                  "WHERE token = %s OR waze_token = %s OR card_token = %s", (kind, token, token, token))
        c.commit()
    return RedirectResponse(row["target_url"], status_code=302)


@app.get("/v1/channels")
def channels():
    """What is switched on, how many people are subscribed, and how the last day's messages fared."""
    with conn() as c:
        subs = c.execute("SELECT channel, count(*) FILTER (WHERE stopped_at IS NULL) AS active, count(*) FILTER (WHERE stopped_at IS NOT NULL) AS stopped "
                         "FROM client.subscriber GROUP BY channel").fetchall()
        box = c.execute("SELECT channel, status, count(*) AS n FROM client.outbox WHERE created_at > now() - INTERVAL '24 hours' GROUP BY channel, status").fetchall()
        opened = c.execute("SELECT count(*) FILTER (WHERE opened_at IS NOT NULL) AS opened, count(*) FILTER (WHERE opened_in = 'waze') AS in_waze, "
                           "count(*) FILTER (WHERE opened_in = 'card') AS in_card, "
                           "count(*) AS sent FROM client.alert_sent WHERE sent_at > now() - INTERVAL '24 hours'").fetchone()
    by = {r["channel"]: {"active": r["active"], "stopped": r["stopped"]} for r in subs}
    day: dict[str, dict[str, int]] = {}
    for r in box:
        day.setdefault(r["channel"], {})[r["status"].lower()] = r["n"]
    return {
        "whatsapp": {"configured": whatsapp.configured(), "template": bool(WHATSAPP_ALERT_TEMPLATE and PUBLIC_BASE_URL), "webhook_secret": bool(WHATSAPP_APP_SECRET),
                     **by.get("WHATSAPP", {"active": 0, "stopped": 0}), "last_24h": day.get("WHATSAPP", {})},
        "telegram": {"configured": telegram.configured(), **by.get("TELEGRAM", {"active": 0, "stopped": 0}), "last_24h": day.get("TELEGRAM", {})},
        "sms": {"provider": SMS_PROVIDER or None, "configured": sms.configured(), "last_24h": day.get("SMS", {})},
        "links": {"short_links": bool(PUBLIC_BASE_URL), "alerts_sent": opened["sent"], "opened": opened["opened"], "opened_in_waze": opened["in_waze"], "opened_live_route": opened["in_card"]},
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


# ---------------------------------------------------------------- subscriber trips: the live route page

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#F2B705"/>'
            '<path d="M120 400V272a88 88 0 0 1 88-88h112v-72l112 112-112 112v-72H208a8 8 0 0 0-8 8v128z" fill="#1E1A05"/></svg>')


class TripReportIn(BaseModel):
    kind: str = Field(pattern="^(WATER|CLOSED|ACCIDENT)$")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class TripConfirmIn(BaseModel):
    zone_id: int
    yes: bool


def _trip_or_404(c, token: str) -> dict:
    t = packs._trip(c, token) if packs.valid_token(token) else None
    if t is None:
        raise HTTPException(404, "trip not found")
    return t


@app.get("/app-icon.svg")
def app_icon():
    return Response(ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/go/{token}")
def trip_page(token: str):
    """A subscriber's own live route: guidance, re-routing, reporting. The token in the link is the only key."""
    if not packs.valid_token(token):
        raise HTTPException(404, "trip not found")
    return FileResponse(STATIC / "card.html")


@app.get("/go/{token}/manifest.webmanifest")
def trip_manifest(token: str):
    """Lets the phone add this trip to the home screen and open it full-screen, like an app."""
    if not packs.valid_token(token):
        raise HTTPException(404, "trip not found")
    return Response(json.dumps({
        "name": "DetourAI live route", "short_name": "DetourAI", "start_url": f"/go/{token}", "scope": f"/go/{token}", "display": "standalone",
        "background_color": "#2B2F33", "theme_color": "#2B2F33",
        "icons": [{"src": "/app-icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}), media_type="application/manifest+json")


@app.get("/v1/trips/{token}")
def trip_pack(token: str, rebuild: bool = False):
    try:
        return packs.trip_get(token, rebuild)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (ValueError, routing.NoRoute) as e:
        raise HTTPException(409, str(e)) from None


@app.get("/v1/trips/{token}/status")
def trip_status(token: str):
    try:
        return packs.trip_status(token)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@app.post("/v1/trips/{token}/report")
def trip_report(token: str, body: TripReportIn):
    """One tap on the live route page: what the person sees, at the phone's position. Same rules as REPORT in chat."""
    from .channels import reports
    with conn() as c:
        t = _trip_or_404(c, token)
        res = reports.create(c, t["sub_id"], body.kind, body.lat, body.lng)
        c.commit()
    return {"ok": res["ok"], "reason": res.get("reason"), "merged": res.get("merged", False), "road": res.get("road")}


@app.post("/v1/trips/{token}/confirm")
def trip_confirm(token: str, body: TripConfirmIn):
    """'Still blocked?' answered on the page. Counts only when this trip's corridor passes the zone."""
    from .channels import reports
    with conn() as c:
        t = _trip_or_404(c, token)
        passes = c.execute(
            "SELECT 1 AS ok FROM client.corridor k JOIN geo.zone_of_avoidance z ON z.zone_id = %s AND k.cells_r9 && z.h3_cover_r9 WHERE k.corr_id = %s",
            (body.zone_id, t["corr_id"])).fetchone()
        if not passes:
            return {"counted": False}
        c.execute("INSERT INTO client.zone_check (sub_id, zone_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (t["sub_id"], body.zone_id))
        res = reports.vote(c, body.zone_id, t["sub_id"], body.yes)
        c.commit()
    return {"counted": res["effect"] != "IGNORED", "status": res["status"]}


# ---------------------------------------------------------------- riders correct the route; a new way from here

class TripFeedbackIn(BaseModel):
    kind: str = Field(pattern="^(BLOCKED|GATE_CLOSED|TOO_NARROW|NO_ENTRY|CHOICE)$")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


def _avoid(avoid: str | None) -> list[int]:
    return [int(x) for x in (avoid or "").split(",") if x.strip().isdigit()][:20]


@app.post("/v1/trips/{token}/feedback")
def trip_feedback(token: str, body: TripFeedbackIn):
    """What was wrong with the route, at the phone's position. 'Blocked' also becomes a road report; the rest wait for
    an operator. The answer names the stretch so the person's next route can avoid it at once."""
    from .channels import reports
    with conn() as c:
        t = _trip_or_404(c, token)
        res = feedback.record(c, t["sub_id"], body.kind, packs.TRIP_PROFILE.get(t["profile"], "2W"), body.lat, body.lng)
        if res["ok"] and body.kind == "BLOCKED":
            reports.create(c, t["sub_id"], "CLOSED", body.lat, body.lng)
            c.commit()
    return res


@app.get("/v1/trips/{token}/reroute")
def trip_reroute(token: str, lat: float = Query(..., ge=-90, le=90), lng: float = Query(..., ge=-180, le=180), avoid: str | None = None):
    try:
        return packs.trip_reroute(token, (lat, lng), _avoid(avoid))
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (ValueError, routing.NoRoute) as e:
        raise HTTPException(409, str(e)) from None


@app.get("/v1/packs/{route_id}/reroute")
def pack_reroute(route_id: int, lat: float = Query(..., ge=-90, le=90), lng: float = Query(..., ge=-180, le=180), avoid: str | None = None):
    try:
        return packs.team_reroute(route_id, (lat, lng), _avoid(avoid))
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (ValueError, routing.NoRoute) as e:
        raise HTTPException(409, str(e)) from None


class CorrectionIn(BaseModel):
    osm_way_id: int
    seg: int
    kind: str = Field(pattern="^(GATE_CLOSED|TOO_NARROW|NO_ENTRY|SKIPPED)$")
    action: str = Field(pattern="^(apply|dismiss)$")
    decide_as: str | None = Field(None, pattern="^(GATE_CLOSED|TOO_NARROW|NO_ENTRY)$", description="for a skipped stretch: what it is")


@app.get("/v1/corrections")
def corrections():
    """What riders say is wrong with the map, grouped by road stretch, with what applying each would do."""
    with conn() as c:
        return {"corrections": feedback.open_corrections(c)}


@app.post("/v1/corrections")
def correction_close(body: CorrectionIn, x_admin_token: Annotated[str | None, Header()] = None):
    _admin(x_admin_token)
    with conn() as c:
        try:
            return feedback.close(c, body.osm_way_id, body.seg, body.kind, body.action, decide_as=body.decide_as)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None


# ---------------------------------------------------------------- what happens on the live route, with consent

class TripEventsIn(BaseModel):
    events: list[dict] = Field(max_length=usage.MAX_BATCH)


@app.post("/v1/trips/{token}/events")
def trip_events(token: str, body: TripEventsIn):
    """Start, switch, leaving the route and arrival, sent by the page only when the rider agreed to share them."""
    with conn() as c:
        t = _trip_or_404(c, token)
        return usage.record(c, t, body.events, packs.TRIP_PROFILE.get(t["profile"], "2W"))


@app.get("/v1/usage")
def usage_summary(days: int = Query(7, ge=1, le=90)):
    """For the product owner: trips started and finished on the live route, switches, departures, and which app
    alert links were opened in."""
    with conn() as c:
        return usage.summary(c, days)
