"""IR -> Zone of Avoidance (blueprint 1.6) and zone lifecycle (1.7).

Geometry is computed in SQL from resolved gazetteer features; the only inputs are the IR,
the message metadata and the road graph, so the same post always yields the same polygon.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import gazetteer
from .config import CITY_ID
from .gazetteer import Anchor, Unresolved

BUFFER_M = {"WATERLOGGING": 80, "VIP": 250, "ROADWORK": 30, "ACCIDENT": 60, "PROCESSION": 150, "GATE": 15, "TREE_FALL": 40, "UNKNOWN": 50}
TTL_MIN = {"WATERLOGGING": 180, "VIP": 45, "ROADWORK": 720, "ACCIDENT": 90, "PROCESSION": 240, "GATE": 1440, "TREE_FALL": 240, "UNKNOWN": 60}
PENALTY = {"WATERLOGGING": 1.0, "VIP": 1.0, "ROADWORK": 0.6, "ACCIDENT": 0.7, "PROCESSION": 1.0, "GATE": 1.0, "TREE_FALL": 0.8, "UNKNOWN": 0.4}
CLASS_RADIUS = {"UNDERPASS": 120, "JUNCTION": 90, "SIGNAL": 90, "FLYOVER": 60, "BRIDGE": 60, "LANDMARK": 80, "GATE": 30, "ROAD": 0, "AREA": 0}
SCOPE_MASK = {"ALL": 127, "2W": 1, "3W": 2, "4W": 12, "LCV": 16, "HCV": 32, "BUS": 64}
ACTIVE_MIN_CONF = 0.75
CANDIDATE_MIN_CONF = 0.50
MERGE_OVERLAP = 0.60


@dataclass
class Zone:
    event_type: str
    cause: str
    geom_ewkt: str
    geom_utm_ewkt: str
    h3_cover: list[int]
    bearing_center: float | None
    bearing_halfwidth: float | None
    profile_mask: int
    penalty_factor: float
    confidence: float
    valid_from: datetime
    valid_to: datetime
    anchors: list[Anchor] = field(default_factory=list)
    method: str = ""
    spine_ewkt: str | None = None      # the line or point the zone was drawn around (what the CIFS feed publishes)
    origin: str = "ADVISORY"


def scope_to_mask(scope: list[str] | None) -> int:
    m = 0
    for s in scope or ["ALL"]:
        m |= SCOPE_MASK.get(s, 0)
    return m or 127


def _utm_srid(c, city_id: int) -> int:
    row = c.execute("SELECT utm_srid FROM geo.city WHERE city_id = %s", (city_id,)).fetchone()
    return int(row["utm_srid"]) if row else 32644


def _nearest_node(c, geom_ewkt: str) -> int:
    row = c.execute(
        "SELECT node_id FROM net.node ORDER BY geom <-> ST_Centroid(ST_GeomFromEWKT(%s)) LIMIT 1", (geom_ewkt,)
    ).fetchone()
    return int(row["node_id"])


def _corridor(c, city_id: int, s: int, t: int, exclude_gaz: int | None) -> str | None:
    """Shortest arterial path s -> t with the VIA road removed from the graph (DISPLACED_CORRIDOR)."""
    row = c.execute(
        """
        WITH p AS (
          SELECT edge FROM pgr_dijkstra(
            format('SELECT id, source, target, cost, reverse_cost FROM net.edge_pgr
                    WHERE city_id = %%s AND highway_rank <= 4 AND road_gaz_id IS DISTINCT FROM %%s',
                   %(city)s::int, COALESCE(%(via)s::bigint, -1)),
            %(s)s::bigint, %(t)s::bigint, true)
          WHERE edge <> -1)
        SELECT ST_AsEWKT(ST_LineMerge(ST_Collect(e.geom))) AS g FROM p JOIN net.edge e ON e.edge_id = p.edge
        """,
        {"city": city_id, "via": exclude_gaz, "s": s, "t": t},
    ).fetchone()
    return row["g"] if row and row["g"] else None


def _buffer_zone(c, src_ewkt: str, srid: int, buffer_m: float, cut_ewkt: str | None) -> tuple[str, str]:
    row = c.execute(
        """
        SELECT ST_AsEWKT(ST_Transform(z, 4326)) AS geom, ST_AsEWKT(z) AS geom_utm
        FROM (SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Difference(
                -- a flat end cap on a point buffers to nothing, so points and areas get a round buffer
                CASE WHEN ST_Dimension(ST_GeomFromEWKT(%(src)s)) = 1
                     THEN ST_Buffer(ST_Transform(ST_GeomFromEWKT(%(src)s), %(srid)s), %(buf)s, 'endcap=flat join=round')
                     ELSE ST_Buffer(ST_Transform(ST_GeomFromEWKT(%(src)s), %(srid)s), %(buf)s) END,
                COALESCE(ST_Buffer(ST_Transform(ST_GeomFromEWKT(%(cut)s), %(srid)s), 25),
                         ST_GeomFromText('POLYGON EMPTY', %(srid)s)))), 3)) AS z) q
        """,
        {"src": src_ewkt, "srid": srid, "buf": buffer_m, "cut": cut_ewkt},
    ).fetchone()
    return row["geom"], row["geom_utm"]


def _h3_cover(c, geom_ewkt: str) -> list[int]:
    """Every res-9 cell the zone touches. polygon_to_cells alone keeps only cells whose centre is inside, which is
    nothing at all for a zone smaller than a cell (a 170 m buffer round a junction), so boundary cells are added."""
    row = c.execute(
        """
        WITH z AS (SELECT ST_GeomFromEWKT(%s) AS g),
        cells AS (
          SELECT h3_polygon_to_cells(d.geom, 9) AS cell FROM z, ST_Dump(z.g) d
          UNION
          SELECT h3_lat_lng_to_cell(p.geom, 9) FROM z, ST_DumpPoints(ST_Segmentize(ST_Boundary(z.g), 0.0005)) p
          UNION
          SELECT h3_lat_lng_to_cell(ST_PointOnSurface(z.g), 9) FROM z)
        SELECT array_agg(DISTINCT cell::bigint) FILTER (WHERE cell IS NOT NULL) AS cells FROM cells
        """,
        (geom_ewkt,),
    ).fetchone()
    return [int(x) for x in (row["cells"] or [])]


def _bearing(c, a_ewkt: str, b_ewkt: str) -> float:
    row = c.execute(
        "SELECT degrees(ST_Azimuth(ST_Centroid(ST_GeomFromEWKT(%s))::geography, ST_Centroid(ST_GeomFromEWKT(%s))::geography)) AS b",
        (a_ewkt, b_ewkt),
    ).fetchone()
    return float(row["b"] or 0.0)


def resolve_zone(c, ir: dict, ts_published: datetime, source_cred: float, city_id: int = CITY_ID,
                 beat: str | None = None) -> Zone:
    """Blueprint 1.6. Raises gazetteer.Unresolved when an anchor cannot be placed."""
    by_role: dict[str, dict] = {}
    for a in ir["anchors"]:
        by_role.setdefault(a["role"], a)
    srid = _utm_srid(c, city_id)
    cause = ir["cause"] if ir["cause"] in BUFFER_M else "UNKNOWN"
    anchors: list[Anchor] = []

    via = gazetteer.resolve(c, by_role["VIA"]["text"], city_id, None, beat) if "VIA" in by_role else None
    if via:
        anchors.append(via)
    near = via.geom_wkt if via else None
    within = via.geom_wkt if via and via.feature_class == "ROAD" else None

    def res(role: str) -> Anchor | None:
        if role not in by_role:
            return None
        a = gazetteer.resolve(c, by_role[role]["text"], city_id, near, beat, within)
        anchors.append(a)
        return a

    frm, to = res("FROM"), res("TO")
    a_, b_ = res("BETWEEN_A"), res("BETWEEN_B")
    at = res("AT")

    bearing: float | None = None
    halfwidth: float | None = None
    method = ""

    if ir["event_type"] in ("DIVERSION", "CLOSURE") and frm is not None and to is not None:
        if via:
            rejoin = c.execute("SELECT ST_AsEWKT(ST_ClosestPoint(ST_GeomFromEWKT(%s), ST_Centroid(ST_GeomFromEWKT(%s)))) AS g",
                               (via.geom_wkt, to.geom_wkt)).fetchone()["g"]
        else:
            rejoin = c.execute("SELECT ST_AsEWKT(ST_ClosestPoint(ST_GeomFromEWKT(%s), ST_Centroid(ST_GeomFromEWKT(%s)))) AS g",
                               (to.geom_wkt, frm.geom_wkt)).fetchone()["g"]
        corridor = _corridor(c, city_id, _nearest_node(c, frm.geom_wkt), _nearest_node(c, rejoin), via.gaz_id if via else None)
        if corridor:
            spine = corridor
            geom, geom_utm = _buffer_zone(c, corridor, srid, BUFFER_M[cause], via.geom_wkt if via else None)
            bearing, halfwidth, method = _bearing(c, frm.geom_wkt, rejoin), 45.0, "displaced_corridor"
        else:
            spine = frm.geom_wkt
            geom, geom_utm = _buffer_zone(c, frm.geom_wkt, srid, CLASS_RADIUS.get(frm.feature_class, 60) + BUFFER_M[cause], None)
            method = "from_anchor_fallback"
    elif a_ is not None and b_ is not None:
        corridor = _corridor(c, city_id, _nearest_node(c, a_.geom_wkt), _nearest_node(c, b_.geom_wkt), None)
        src = corridor or a_.geom_wkt
        spine = src
        geom, geom_utm = _buffer_zone(c, src, srid, BUFFER_M[cause] + (0 if corridor else CLASS_RADIUS.get(a_.feature_class, 60)), None)
        method = "between_corridor" if corridor else "between_a_fallback"
    else:
        anchor = at or frm or to or (anchors[0] if anchors else None)
        if anchor is None:
            raise Unresolved(ir["anchors"][0]["text"], [])
        spine = anchor.geom_wkt
        geom, geom_utm = _buffer_zone(c, anchor.geom_wkt, srid, CLASS_RADIUS.get(anchor.feature_class, 60) + BUFFER_M[cause], None)
        method = f"single_{anchor.feature_class.lower()}"

    tw = ir.get("time_window") or {}
    valid_from = ts_published
    valid_to = ts_published + timedelta(minutes=TTL_MIN[cause])
    if tw.get("end"):
        valid_to = max(valid_to, ts_published + timedelta(minutes=TTL_MIN[cause]))   # typed clock times are parsed in a later slice

    return Zone(
        event_type=ir["event_type"], cause=cause, geom_ewkt=geom, geom_utm_ewkt=geom_utm,
        h3_cover=_h3_cover(c, geom), bearing_center=bearing, bearing_halfwidth=halfwidth,
        profile_mask=scope_to_mask(ir.get("vehicle_scope")), penalty_factor=PENALTY[cause],
        confidence=round(float(ir["confidence"]) * float(source_cred) ** 0.5, 3),
        valid_from=valid_from, valid_to=valid_to, anchors=anchors, method=method, spine_ewkt=spine,
    )


def commit_zone(c, z: Zone, ir_id: int | None, city_id: int = CITY_ID, force_status: str | None = None) -> dict:
    """Insert with the 1.7 status rule, merge into an overlapping active zone of the same cause, project onto edges."""
    status = force_status or ("ACTIVE" if z.confidence >= ACTIVE_MIN_CONF else "CANDIDATE" if z.confidence >= CANDIDATE_MIN_CONF else "DROPPED")
    if status == "DROPPED":
        return {"status": "DROPPED", "zone_id": None, "edges_updated": 0}

    merged = c.execute(
        """
        SELECT zone_id FROM geo.zone_of_avoidance z
        WHERE z.city_id = %(city)s AND z.cause = %(cause)s AND z.status IN ('ACTIVE','DECAYING','CANDIDATE') AND z.valid_to > now()
          AND z.h3_cover_r9 && %(cells)s::h3index[]
          AND ST_Area(ST_Intersection(z.geom, ST_GeomFromEWKT(%(g)s)))
              / NULLIF(LEAST(ST_Area(z.geom), ST_Area(ST_GeomFromEWKT(%(g)s))), 0) >= %(ov)s
        ORDER BY z.zone_id LIMIT 1
        """,
        {"city": city_id, "cause": z.cause, "cells": z.h3_cover, "g": z.geom_ewkt, "ov": MERGE_OVERLAP},
    ).fetchone()
    if merged:
        zid = int(merged["zone_id"])
        c.execute(
            """
            UPDATE geo.zone_of_avoidance SET
              corroborations = corroborations + 1,
              confidence = LEAST(1.0, GREATEST(confidence, %(conf)s) + 0.05),
              valid_to = GREATEST(valid_to, %(vto)s),
              geom = ST_Multi(ST_Union(geom, ST_GeomFromEWKT(%(g)s))),
              geom_utm = ST_Multi(ST_Union(geom_utm, ST_GeomFromEWKT(%(gu)s))),
              h3_cover_r9 = ARRAY(SELECT DISTINCT unnest(h3_cover_r9 || %(cells)s::h3index[])),
              status = CASE WHEN status = 'CANDIDATE' THEN 'ACTIVE' ELSE status END
            WHERE zone_id = %(zid)s
            """,
            {"conf": z.confidence, "vto": z.valid_to, "g": z.geom_ewkt, "gu": z.geom_utm_ewkt, "cells": z.h3_cover, "zid": zid},
        )
        n = c.execute("SELECT geo.apply_zone(%s) AS n", (zid,)).fetchone()["n"]
        c.commit()
        return {"status": "MERGED", "zone_id": zid, "edges_updated": int(n)}

    row = c.execute(
        """
        INSERT INTO geo.zone_of_avoidance
          (city_id, ir_id, event_type, cause, geom, geom_utm, h3_cover_r9, bearing_center, bearing_halfwidth,
           profile_mask, penalty_factor, confidence, status, valid_from, valid_to, spine, origin)
        VALUES (%(city)s, %(ir)s, %(et)s, %(cause)s, ST_GeomFromEWKT(%(g)s), ST_GeomFromEWKT(%(gu)s), %(cells)s::h3index[],
                %(bc)s, %(bh)s, %(mask)s, %(pen)s, %(conf)s, %(status)s, %(vf)s, %(vt)s, ST_GeomFromEWKT(%(spine)s), %(origin)s)
        RETURNING zone_id
        """,
        {"city": city_id, "ir": ir_id, "et": z.event_type, "cause": z.cause, "g": z.geom_ewkt, "gu": z.geom_utm_ewkt,
         "cells": z.h3_cover, "bc": z.bearing_center, "bh": z.bearing_halfwidth, "mask": z.profile_mask,
         "pen": z.penalty_factor, "conf": z.confidence, "status": status, "vf": z.valid_from, "vt": z.valid_to,
         "spine": z.spine_ewkt, "origin": z.origin},
    ).fetchone()
    zid = int(row["zone_id"])
    n = 0
    if status == "ACTIVE":
        n = int(c.execute("SELECT geo.apply_zone(%s) AS n", (zid,)).fetchone()["n"])
    c.commit()
    return {"status": status, "zone_id": zid, "edges_updated": n}


def set_status(c, zone_id: int, status: str, reviewed_by: str | None = None) -> int:
    c.execute("UPDATE geo.zone_of_avoidance SET status = %s, reviewed_by = COALESCE(%s, reviewed_by) WHERE zone_id = %s",
              (status, reviewed_by, zone_id))
    n = int(c.execute("SELECT geo.reapply_all_zones() AS n").fetchone()["n"])
    c.commit()
    return n


def expire(c) -> dict:
    """1.7 decay: DECAYING in the last 30 min, EXPIRED after valid_to; then re-project every edge."""
    decaying = c.execute(
        "UPDATE geo.zone_of_avoidance SET status = 'DECAYING', penalty_factor = penalty_factor * 0.5 "
        "WHERE status = 'ACTIVE' AND valid_to - INTERVAL '30 minutes' < now() AND valid_to > now() RETURNING zone_id"
    ).rowcount
    expired = c.execute(
        "UPDATE geo.zone_of_avoidance SET status = 'EXPIRED' WHERE status IN ('ACTIVE','DECAYING','CANDIDATE') AND valid_to <= now() RETURNING zone_id"
    ).rowcount
    n = int(c.execute("SELECT geo.reapply_all_zones() AS n").fetchone()["n"])
    c.commit()
    return {"decaying": decaying, "expired": expired, "edges_reprojected": n}


def zone_from_geometry(c, geom_geojson: dict, event_type: str, cause: str, penalty: float, confidence: float,
                       valid_from: datetime, valid_to: datetime, city_id: int = CITY_ID, buffer_m: float | None = None) -> Zone:
    """A zone from a source that already carries geometry (TomTom incidents): buffer the line or point, no language step."""
    import json as _json
    srid = _utm_srid(c, city_id)
    cause = cause if cause in BUFFER_M else "UNKNOWN"
    src = c.execute("SELECT ST_AsEWKT(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)) AS g", (_json.dumps(geom_geojson),)).fetchone()["g"]
    radius = buffer_m if buffer_m is not None else (BUFFER_M[cause] if geom_geojson["type"] == "LineString" else BUFFER_M[cause] + 60)
    geom, geom_utm = _buffer_zone(c, src, srid, min(radius, 60) if geom_geojson["type"] == "LineString" else radius, None)
    return Zone(event_type=event_type, cause=cause, geom_ewkt=geom, geom_utm_ewkt=geom_utm, h3_cover=_h3_cover(c, geom),
                bearing_center=None, bearing_halfwidth=None, profile_mask=127, penalty_factor=penalty,
                confidence=round(confidence, 3), valid_from=valid_from, valid_to=valid_to, anchors=[], method="source_geometry",
                spine_ewkt=src, origin="FEED")


def lift_near(c, anchor: Anchor, radius_m: float = 300.0, city_id: int = CITY_ID) -> list[int]:
    """A 'restored / cleared' advisory refutes the active zones around its anchor."""
    rows = c.execute(
        """
        UPDATE geo.zone_of_avoidance z SET status = 'REFUTED'
        WHERE z.city_id = %s AND z.status IN ('ACTIVE','DECAYING','CANDIDATE') AND z.valid_to > now()
          AND ST_DWithin(z.geom::geography, ST_GeomFromEWKT(%s)::geography, %s)
        RETURNING zone_id
        """,
        (city_id, anchor.geom_wkt, radius_m),
    ).fetchall()
    if rows:
        c.execute("SELECT geo.reapply_all_zones()")
    c.commit()
    return [int(r["zone_id"]) for r in rows]
