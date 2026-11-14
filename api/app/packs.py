"""Route packs: a saved trip made navigable without a network (Offline guidance tab).

A pack holds up to three routes (A best now, B and C dissimilar alternates), each with its line, turn list,
time and the decision point where it leaves route A. The server can later say which of the three is best now.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import maps, profiles, routing
from .config import CITY_ID
from .db import conn

MAX_SHARED = 0.40                 # an alternate may share at most this fraction of its own length with an earlier route
PENALTIES = (2.5, 5.0, 10.0)      # tried in order until the alternate is dissimilar enough
MAX_SLOWER = 2.2                  # an alternate more than this many times slower than A is not offered
STALE_OVERLAP = 0.80              # below this overlap with every stored route, the pack needs rebuilding
MAX_AGE_S = 6 * 3600
LABELS = ("A", "B", "C")


# ---------------------------------------------------------------- pure helpers (unit-tested)

def shared_fraction(candidate: list[dict], other_ids: set[int]) -> float:
    """Share of the candidate's length that runs on edges of another route."""
    total = sum(float(e["length_m"]) for e in candidate)
    if total <= 0:
        return 1.0
    return sum(float(e["length_m"]) for e in candidate if e["edge_id"] in other_ids) / total


def decision_index(a_ids: list[int], b_ids: list[int]) -> int:
    """Index of the first edge where b leaves a (length of the common prefix)."""
    i = 0
    while i < len(a_ids) and i < len(b_ids) and a_ids[i] == b_ids[i]:
        i += 1
    return i


def _deflection(b1: float, b2: float) -> float:
    return ((b2 - b1 + 540.0) % 360.0) - 180.0          # negative = left, positive = right


def _verb(d: float) -> str | None:
    a = abs(d)
    side = "right" if d > 0 else "left"
    if a < 30:
        return None
    if a < 60:
        return f"Bear {side}"
    if a < 150:
        return f"Turn {side}"
    return "Make a U-turn"


def turn_list(edges: list[dict]) -> list[dict]:
    """Instructions a rider can follow from road names alone. One entry per change of road or real turn."""
    if not edges:
        return []
    out: list[dict] = []
    at = 0.0
    first = edges[0]
    out.append({"at_m": 0, "text": f"Start on {first.get('name') or 'the road ahead'}", "road": first.get("name"),
                "lat": first["start_lat"], "lng": first["start_lng"]})
    for prev, cur in zip(edges, edges[1:], strict=False):
        at += float(prev["length_m"])
        d = _deflection(float(prev["bearing"]), float(cur["bearing"]))
        verb = _verb(d)
        new_road = (cur.get("name") or None) != (prev.get("name") or None)
        if not new_road and (verb is None or abs(d) < 60):
            continue                                       # same road, no real turn
        name = cur.get("name")
        if new_road and name:
            text = f"{verb or 'Continue'} onto {name}"
        elif new_road:
            text = f"{verb or 'Continue'} onto the unnamed road"
        else:
            text = f"{verb} to stay on {name}" if name else verb
        out.append({"at_m": round(at), "text": text, "road": name, "lat": cur["start_lat"], "lng": cur["start_lng"]})
    at += float(edges[-1]["length_m"])
    out.append({"at_m": round(at), "text": "Arrive", "road": None, "lat": None, "lng": None})
    return out


def version_of(edge_lists: dict[str, list[int]]) -> str:
    return hashlib.sha1(json.dumps(edge_lists, sort_keys=True).encode()).hexdigest()[:10]


def best_label(current: list[dict], stored: dict[str, list[list]]) -> tuple[str | None, float]:
    """Which stored route the current best route matches most, by shared length of the current route."""
    best, score = None, 0.0
    for label, pairs in stored.items():
        s = shared_fraction(current, {int(p[0]) for p in pairs})
        if s > score:
            best, score = label, s
    return best, score


# ---------------------------------------------------------------- build and status

def _route_payload(label: str, r: routing.RouteResult, a_ids: list[int] | None) -> dict:
    ids = [e["edge_id"] for e in r.edges]
    turns = turn_list(r.edges)
    dp = None
    if a_ids is not None:
        i = decision_index(a_ids, ids)
        if 0 < i < len(r.edges):
            e = r.edges[i]
            dp = {"at_m": round(sum(float(x["length_m"]) for x in r.edges[:i])), "lat": e["start_lat"], "lng": e["start_lng"],
                  "onto": e.get("name")}
        elif i == 0:
            dp = {"at_m": 0, "lat": r.edges[0]["start_lat"], "lng": r.edges[0]["start_lng"], "onto": r.edges[0].get("name")}
    names: dict[str, float] = {}
    for e in r.edges:
        if e.get("name"):
            names[e["name"]] = names.get(e["name"], 0.0) + float(e["length_m"])
    return {"label": label, "minutes": round(r.duration_s / 60), "km": round(r.distance_m / 1000, 1),
            "via": sorted(names, key=names.get, reverse=True)[:3], "geometry": r.geometry, "turns": turns,
            "decision_point": dp}


def build(route_id: int, city_id: int = CITY_ID) -> dict:
    with conn() as c:
        tr = c.execute(
            "SELECT route_id, name, profile, origin_label, dest_label, ST_Y(origin) olat, ST_X(origin) olng, ST_Y(dest) dlat, ST_X(dest) dlng "
            "FROM client.team_route WHERE route_id = %s AND city_id = %s AND active", (route_id, city_id)).fetchone()
    if tr is None:
        raise LookupError("team route not found")
    p = profiles.get(tr["profile"])
    o, d = (tr["olat"], tr["olng"]), (tr["dlat"], tr["dlng"])

    found: list[tuple[str, routing.RouteResult]] = [("A", routing.route(p, o, d))]
    for label in LABELS[1:]:
        taken = [e["edge_id"] for _, r in found for e in r.edges]
        for pen in PENALTIES:
            try:
                cand = routing.route(p, o, d, penalize=taken, penalty=pen)
            except routing.NoRoute:
                break
            if cand.duration_s > MAX_SLOWER * found[0][1].duration_s:
                break
            if all(shared_fraction(cand.edges, {e["edge_id"] for e in r.edges}) <= MAX_SHARED for _, r in found):
                found.append((label, cand))
                break

    a_ids = [e["edge_id"] for e in found[0][1].edges]
    routes = {label: _route_payload(label, r, None if label == "A" else a_ids) for label, r in found}
    edge_lists = {label: [e["edge_id"] for e in r.edges] for label, r in found}
    edges = {label: [[e["edge_id"], float(e["length_m"])] for e in r.edges] for label, r in found}
    try:
        usual = round(routing.route(p, o, d, use_zones=False).duration_s / 60)
    except routing.NoRoute:
        usual = routes["A"]["minutes"]

    xs = [pt[0] for r in routes.values() for pt in r["geometry"]["coordinates"]]
    ys = [pt[1] for r in routes.values() for pt in r["geometry"]["coordinates"]]
    with conn() as c:
        zrows = c.execute(
            """
            SELECT z.zone_id, z.cause, z.status, z.valid_to, ST_AsGeoJSON(z.geom)::json AS geometry
            FROM geo.zone_of_avoidance z
            WHERE z.city_id = %s AND z.status IN ('ACTIVE','DECAYING') AND z.valid_to > now()
              AND z.geom && ST_Expand(ST_MakeEnvelope(%s, %s, %s, %s, 4326), 0.01)
            """,
            (city_id, min(xs), min(ys), max(xs), max(ys)),
        ).fetchall()
        pack = {
            "route_id": tr["route_id"], "name": tr["name"], "profile": p.code, "version": version_of(edge_lists),
            "built_at": datetime.now(timezone.utc).isoformat(), "usual_minutes": usual, "best": "A",
            "from": {"lat": o[0], "lng": o[1], "label": tr["origin_label"]}, "to": {"lat": d[0], "lng": d[1], "label": tr["dest_label"]},
            "routes": routes,
            "zones": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": z["geometry"],
                 "properties": {"zone_id": z["zone_id"], "cause": z["cause"], "status": z["status"], "valid_to": z["valid_to"].isoformat()}}
                for z in zrows]},
            "maps_url": maps.maps_url(p.code, d, maps.waypoints(found[0][1].edges), o),
        }
        c.execute(
            """
            INSERT INTO client.route_pack (route_id, version, pack, edges) VALUES (%s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (route_id) DO UPDATE SET version = EXCLUDED.version, built_at = now(), pack = EXCLUDED.pack, edges = EXCLUDED.edges
            """,
            (route_id, pack["version"], json.dumps(pack), json.dumps(edges)))
        c.commit()
    return pack


def get(route_id: int, rebuild: bool = False) -> dict:
    if not rebuild:
        with conn() as c:
            row = c.execute(
                "SELECT pack FROM client.route_pack WHERE route_id = %s AND built_at > now() - make_interval(secs => %s)",
                (route_id, MAX_AGE_S)).fetchone()
        if row:
            return row["pack"]
    return build(route_id)


def status(route_id: int, city_id: int = CITY_ID) -> dict:
    """Which of the stored routes is best right now. stale=True tells the card to fetch a fresh pack."""
    with conn() as c:
        row = c.execute(
            """
            SELECT k.version, k.edges, t.profile, ST_Y(t.origin) olat, ST_X(t.origin) olng, ST_Y(t.dest) dlat, ST_X(t.dest) dlng
            FROM client.route_pack k JOIN client.team_route t USING (route_id)
            WHERE k.route_id = %s AND t.city_id = %s AND t.active
            """,
            (route_id, city_id)).fetchone()
    if row is None:
        raise LookupError("no pack built for this route yet")
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = routing.route(profiles.get(row["profile"]), (row["olat"], row["olng"]), (row["dlat"], row["dlng"]))
    except routing.NoRoute:
        return {"version": row["version"], "best": None, "stale": False, "blocked": True, "minutes": None, "checked_at": now}
    label, overlap = best_label(cur.edges, row["edges"])
    return {"version": row["version"], "best": label if overlap >= STALE_OVERLAP else None, "stale": overlap < STALE_OVERLAP,
            "blocked": False, "minutes": round(cur.duration_s / 60), "overlap": round(overlap, 2), "checked_at": now}
