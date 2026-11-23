"""Profile-aware routing on pgRouting over net.* with the net.edge_dynamic overlay.

v0 of Module 2: the DimensionalWeighting formula from the blueprint is expressed as the edge-cost
SQL that pgr_dijkstra consumes. Hard exclusions are cost -1 (pgRouting treats negative cost as
impassable); soft penalties multiply travel time. Turn restrictions (net.turn) are not applied
here; they arrive with GraphHopper.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass

import psycopg

from .config import (
    BBOX_PAD_FRACTION,
    FILTER_SPEED_CAP_KMH,
    NARROW_TWO_WAY_MULT,
    MIN_PAD_DEG,
    ROUTE_BBOX_RETRIES,
    SUBMERGE_MULT,
    UNKNOWN_GATE_COST_S,
    ZONE_SOFT_MULT,
)
from .db import conn
from .profiles import Profile


class NoRoute(Exception):
    pass


@dataclass
class Snap:
    node_id: int
    edge_id: int
    lat: float
    lng: float
    dist_m: float            # straight-line distance from the requested point to the snapped point on the road


@dataclass
class RouteResult:
    profile: str
    src_node: int
    dst_node: int
    distance_m: float
    duration_s: float
    geometry: dict            # GeoJSON LineString, origin -> snap -> ... -> snap -> destination
    edges: list[dict]         # ordered per-edge detail (edge_id, length_m, highway_rank, name, zone_adjacent, gate_state, mid_lat, mid_lng)
    snap_from: Snap
    snap_to: Snap
    bbox_expansions: int


def _bbox(origin: tuple[float, float], dest: tuple[float, float], factor: float = 1.0) -> tuple[float, float, float, float]:
    (lat1, lng1), (lat2, lng2) = origin, dest
    pad = max(MIN_PAD_DEG, BBOX_PAD_FRACTION * math.hypot(lat2 - lat1, lng2 - lng1)) * factor
    return (min(lng1, lng2) - pad, min(lat1, lat2) - pad, max(lng1, lng2) + pad, max(lat1, lat2) + pad)


def _edge_sql(p: Profile, bbox: tuple[float, float, float, float], use_zones: bool = True,
              penalize: list[int] | None = None, penalty: float = 1.0) -> str:
    """Edge-cost SQL for pgr_dijkstra. All values interpolated are numbers or SQL literals from the profile table."""
    bit = int(p.bit)
    passing_w = round(2 * float(p.width_m) + 0.4, 2)     # room for two of this vehicle side by side; narrower two-way lanes are single file
    maxspeed = float(p.max_speed_kmh)
    clearance = float(p.ground_clearance_m)
    can_filter = "true" if p.can_filter else "false"
    minx, miny, maxx, maxy = (float(v) for v in bbox)
    # alternates: make the edges of routes already chosen more expensive so the next search goes elsewhere
    pen = ""
    if penalize and penalty > 1.0:
        ids = ",".join(str(int(i)) for i in penalize)
        pen = f" * CASE WHEN e.edge_id = ANY('{{{ids}}}'::bigint[]) THEN {float(penalty)} ELSE 1 END"

    speed = f"""
        CASE WHEN y.speed_now_kmh IS NOT NULL AND y.speed_now_kmh > 0 THEN
               CASE WHEN {can_filter} AND d.filterable
                    THEN GREATEST(y.speed_now_kmh, LEAST({FILTER_SPEED_CAP_KMH}, LEAST(e.freeflow_kmh, {maxspeed}) * 0.6))
                    ELSE LEAST(e.freeflow_kmh, y.speed_now_kmh, {maxspeed}) END
             ELSE LEAST(e.freeflow_kmh, {maxspeed}) END"""

    def cost(zone_penalty: str) -> str:
        return f"""
        CASE WHEN (d.static_mask & {bit}) = 0 THEN -1
             WHEN y.gate_state IN ('CLOSED_REPORTED','CLOSED_INFERRED') AND NOT ({bit} = 1 AND y.two_wheeler_gap) THEN -1
             WHEN y.depth_est_m >= {clearance} THEN -1
             WHEN (y.zone_mask & {bit}) > 0 AND {zone_penalty} >= 1.0 THEN -1
             ELSE ((e.length_m / (({speed}) / 3.6))
                  * (1 + {ZONE_SOFT_MULT} * CASE WHEN (y.zone_mask & {bit}) > 0 THEN {zone_penalty} ELSE 0 END)
                  * (1 + {SUBMERGE_MULT} * y.p_submerge)
                  * CASE WHEN NOT e.oneway AND d.width_m < {passing_w} THEN {NARROW_TWO_WAY_MULT} ELSE 1 END
                  + CASE WHEN y.gate_state = 'UNKNOWN' THEN -ln(GREATEST(y.p_open_now, 0.001)) * {UNKNOWN_GATE_COST_S} ELSE 0 END){pen}
        END"""

    return f"""
        SELECT e.edge_id AS id, e.src AS source, e.dst AS target,
               ({cost('y.zone_penalty_fwd' if use_zones else '0::real')})::float8 AS cost,
               (CASE WHEN e.oneway THEN -1 ELSE {cost('y.zone_penalty_rev' if use_zones else '0::real')} END)::float8 AS reverse_cost
        FROM net.edge e
        JOIN net.edge_dim d USING (edge_id)
        JOIN net.edge_dynamic y USING (edge_id)
        WHERE e.geom && ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}, 4326)
    """


def _snap(c, p: Profile, lat: float, lng: float) -> Snap:
    """Nearest point on the nearest edge the profile may use, and the closer endpoint as the graph entry."""
    row = c.execute(
        """
        WITH q AS (SELECT ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326) AS p),
             e AS (
               SELECT e.edge_id, e.src, e.dst, e.geom
               FROM net.edge e JOIN net.edge_dim d USING (edge_id), q
               WHERE (d.static_mask & %(bit)s) > 0
               ORDER BY e.geom <-> q.p
               LIMIT 1)
        SELECT e.edge_id,
               CASE WHEN ST_LineLocatePoint(e.geom, q.p) < 0.5 THEN e.src ELSE e.dst END AS node_id,
               ST_Y(ST_ClosestPoint(e.geom, q.p)) AS lat, ST_X(ST_ClosestPoint(e.geom, q.p)) AS lng,
               ST_Distance(e.geom::geography, q.p::geography) AS dist_m
        FROM e, q
        """,
        {"lat": lat, "lng": lng, "bit": p.bit},
    ).fetchone()
    if row is None:
        raise NoRoute("no road open to this profile near the point")
    return Snap(int(row["node_id"]), int(row["edge_id"]), float(row["lat"]), float(row["lng"]), float(row["dist_m"]))


_CACHE: "OrderedDict[tuple, RouteResult]" = OrderedDict()
_CACHE_MAX = 1024
_CACHE_LOCK = threading.Lock()
CACHE_STATS = {"hits": 0, "misses": 0}


def _epoch() -> int | None:
    """The change counter from net.dyn_epoch, or None on a database that predates it (then nothing is cached)."""
    try:
        with conn() as c:
            row = c.execute("SELECT epoch FROM net.dyn_epoch").fetchone()
            return int(row["epoch"]) if row else None
    except psycopg.errors.UndefinedTable:
        return None


def route(p: Profile, origin: tuple[float, float], dest: tuple[float, float], use_zones: bool = True,
          penalize: list[int] | None = None, penalty: float = 1.0, fresh: bool = False) -> RouteResult:
    """use_zones=False gives the usual route, as if no zone were active (the brief compares against it).
    penalize/penalty steer the search away from given edges (route packs use it to find alternates).

    Results are remembered per change counter: the same request is answered from memory until a zone, gate, depth,
    width or the network changes, and recomputed after."""
    ep = None if fresh else _epoch()          # fresh=True always computes (the verifier times the real search with it)
    if ep is None:
        return _route(p, origin, dest, use_zones, penalize, penalty)
    key = (p.code, round(origin[0], 5), round(origin[1], 5), round(dest[0], 5), round(dest[1], 5), use_zones,
           tuple(penalize or ()), penalty, ep)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            CACHE_STATS["hits"] += 1
            return hit
    res = _route(p, origin, dest, use_zones, penalize, penalty)          # NoRoute propagates and is not remembered
    with _CACHE_LOCK:
        CACHE_STATS["misses"] += 1
        _CACHE[key] = res
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return res


def _route(p: Profile, origin: tuple[float, float], dest: tuple[float, float], use_zones: bool = True,
           penalize: list[int] | None = None, penalty: float = 1.0) -> RouteResult:
    with conn() as c:
        sf = _snap(c, p, *origin)
        st = _snap(c, p, *dest)
        s, t = sf.node_id, st.node_id
        if s == t:
            raise NoRoute("origin and destination snap to the same node")

        rows: list = []
        expansions = 0
        for expansions in range(ROUTE_BBOX_RETRIES + 1):
            rows = c.execute(
                "SELECT seq, node, edge, cost, agg_cost FROM pgr_bdDijkstra(%s, %s, %s, true) ORDER BY seq",
                (_edge_sql(p, _bbox(origin, dest, 2.0 ** expansions), use_zones, penalize, penalty), s, t),
            ).fetchall()
            if rows:
                break
        if not rows:
            raise NoRoute("no path for this profile; the search window was widened twice")

        edges = [int(r["edge"]) for r in rows if r["edge"] != -1]
        nodes = [int(r["node"]) for r in rows if r["edge"] != -1]
        duration_s = float(rows[-1]["agg_cost"])

        g = c.execute(
            """
            WITH p AS (
              SELECT t.edge, t.node, t.seq FROM unnest(%s::bigint[], %s::bigint[]) WITH ORDINALITY AS t(edge, node, seq)
            ),
            seg AS (
              SELECT p.seq,
                     CASE WHEN p.node = e.src THEN e.geom ELSE ST_Reverse(e.geom) END AS g,
                     e.edge_id, e.length_m, e.highway_rank, e.name,
                     (y.zone_penalty_fwd > 0 OR y.zone_penalty_rev > 0) AS zone_adjacent,
                     y.gate_state
              FROM p
              JOIN net.edge e ON e.edge_id = p.edge
              JOIN net.edge_dynamic y ON y.edge_id = e.edge_id
            )
            SELECT ST_AsGeoJSON(ST_MakeLine(g ORDER BY seq))::json AS geometry,
                   sum(length_m)::float8 AS distance_m,
                   json_agg(json_build_object(
                     'edge_id', edge_id, 'length_m', length_m, 'highway_rank', highway_rank, 'name', name,
                     'zone_adjacent', zone_adjacent, 'gate_state', gate_state,
                     'mid_lat', ST_Y(ST_LineInterpolatePoint(g, 0.5)),
                     'mid_lng', ST_X(ST_LineInterpolatePoint(g, 0.5)),
                     'start_lat', ST_Y(ST_StartPoint(g)), 'start_lng', ST_X(ST_StartPoint(g)),
                     'bearing', COALESCE(degrees(ST_Azimuth(ST_StartPoint(g), ST_EndPoint(g))), 0)) ORDER BY seq) AS edges
            FROM seg
            """,
            (edges, nodes),
        ).fetchone()

    geometry = g["geometry"]
    # connectors from the requested points to the road, so the drawn line reaches the pins
    geometry["coordinates"] = ([[origin[1], origin[0]], [sf.lng, sf.lat]] + geometry["coordinates"]
                               + [[st.lng, st.lat], [dest[1], dest[0]]])
    return RouteResult(
        profile=p.code,
        src_node=s,
        dst_node=t,
        distance_m=float(g["distance_m"]) + sf.dist_m + st.dist_m,
        duration_s=duration_s,
        geometry=geometry,
        edges=g["edges"],
        snap_from=sf,
        snap_to=st,
        bbox_expansions=expansions,
    )
