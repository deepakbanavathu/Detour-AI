"""Active zones as a Waze CIFS feed (Closure and Incident Feed Specification).

A traffic authority registers the feed URL under Waze for Cities; Waze fetches it and shows the closures in Waze
and Google Maps. Rules taken from the specification: a stable id, one street per incident, a polyline that follows
the road in the affected direction, ROAD_CLOSED only when the whole road is shut, a start time that never changes
once active, and a description under 40 characters.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

from .config import CIFS_MIN_CONFIDENCE, CITY_ID

IST = timezone(timedelta(hours=5, minutes=30))
TYPES = {"ROAD_CLOSED", "ACCIDENT", "HAZARD", "POLICE", "CHIT_CHAT", "JAM"}
SUBTYPES = {
    "ROAD_CLOSED": {"ROAD_CLOSED_HAZARD", "ROAD_CLOSED_CONSTRUCTION", "ROAD_CLOSED_EVENT"},
    "ACCIDENT": {"ACCIDENT_MINOR", "ACCIDENT_MAJOR"},
    "HAZARD": {"HAZARD_ON_ROAD", "HAZARD_ON_ROAD_CONSTRUCTION", "HAZARD_ON_ROAD_OBJECT", "HAZARD_WEATHER_FLOOD", "HAZARD_ON_ROAD_LANE_CLOSED"},
}
DESCRIPTION = {"WATERLOGGING": "Waterlogging reported", "VIP": "VIP movement, road closed", "ROADWORK": "Road work",
               "ACCIDENT": "Accident", "PROCESSION": "Procession, road closed", "TREE_FALL": "Fallen tree on road",
               "GATE": "Road closed", "UNKNOWN": "Traffic restriction"}
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
MIN_CLOSURE_M, MAX_POLYLINE_M = 30.0, 20000.0


# ---------------------------------------------------------------- pure helpers (unit-tested)

def cifs_type(event_type: str, cause: str, penalty: float, has_line: bool) -> tuple[str, str | None]:
    """ROAD_CLOSED only for a full closure with a line to close; everything else is shown without changing routes."""
    if cause == "ACCIDENT":
        return "ACCIDENT", None
    if has_line and penalty >= 1.0 and event_type in ("CLOSURE", "DIVERSION", "VIP_MOVEMENT"):
        return "ROAD_CLOSED", {"ROADWORK": "ROAD_CLOSED_CONSTRUCTION", "VIP": "ROAD_CLOSED_EVENT", "PROCESSION": "ROAD_CLOSED_EVENT",
                               "WATERLOGGING": "ROAD_CLOSED_HAZARD", "TREE_FALL": "ROAD_CLOSED_HAZARD"}.get(cause)
    return "HAZARD", {"WATERLOGGING": "HAZARD_WEATHER_FLOOD", "ROADWORK": "HAZARD_ON_ROAD_CONSTRUCTION",
                      "TREE_FALL": "HAZARD_ON_ROAD_OBJECT"}.get(cause, "HAZARD_ON_ROAD")


def ts(t: datetime) -> str:
    return t.astimezone(IST).replace(microsecond=0).isoformat()


def polyline(coords: list[list[float]]) -> str:
    """GeoJSON [lng, lat] pairs to CIFS 'lat lon lat lon', six decimals."""
    return " ".join(f"{lat:.6f} {lng:.6f}" for lng, lat in coords)


def _bearing(a: list[float], b: list[float]) -> float:
    dlng = math.radians(b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    return (math.degrees(math.atan2(dlng, math.radians(b[1] - a[1]))) + 360) % 360


def orient(coords: list[list[float]], bearing: float | None) -> list[list[float]]:
    """Order the points along the affected direction of travel (the spec reads direction from point order)."""
    if bearing is None or len(coords) < 2:
        return coords
    diff = abs(((_bearing(coords[0], coords[-1]) - bearing + 540) % 360) - 180)
    return coords[::-1] if diff > 90 else coords


def length_m(coords: list[list[float]]) -> float:
    total = 0.0
    for a, b in zip(coords, coords[1:], strict=False):
        k = math.cos(math.radians((a[1] + b[1]) / 2)) * 111320
        total += math.hypot((b[0] - a[0]) * k, (b[1] - a[1]) * 110540)
    return total


def validate(inc: dict) -> list[str]:
    """Problems that would make Waze reject or misplace the incident. Empty list = valid."""
    bad = []
    for k in ("id", "type", "polyline", "street"):
        if not str(inc.get(k) or "").strip():
            bad.append(f"missing {k}")
    t = inc.get("type")
    if t not in TYPES:
        bad.append(f"type {t!r} is not a CIFS type")
    if inc.get("subtype") and inc["subtype"] not in SUBTYPES.get(t, set()):
        bad.append(f"subtype {inc['subtype']} does not belong to {t}")
    nums = (inc.get("polyline") or "").split()
    try:
        vals = [float(x) for x in nums]
    except ValueError:
        vals = []
        bad.append("polyline is not numeric")
    if len(vals) < 2 or len(vals) % 2:
        bad.append("polyline needs lat lon pairs")
    elif not all(-90 <= vals[i] <= 90 and -180 <= vals[i + 1] <= 180 for i in range(0, len(vals), 2)):
        bad.append("polyline is not lat lon order")
    else:
        pts = [[vals[i + 1], vals[i]] for i in range(0, len(vals), 2)]
        if t == "ROAD_CLOSED":
            if len(pts) < 2:
                bad.append("a closure needs a line, not a point")
            elif not MIN_CLOSURE_M <= length_m(pts) <= MAX_POLYLINE_M:
                bad.append(f"closure length {length_m(pts):.0f} m is outside 30 m to 20 km")
        if len(pts) == 1 and inc.get("direction") not in ("BOTH_DIRECTIONS", "ONE_DIRECTION"):
            bad.append("a single point needs a direction")
    if inc.get("direction") and inc["direction"] not in ("BOTH_DIRECTIONS", "ONE_DIRECTION"):
        bad.append("direction must be BOTH_DIRECTIONS or ONE_DIRECTION")
    if t == "ROAD_CLOSED" and not inc.get("starttime"):
        bad.append("a closure needs a starttime")
    for k in ("starttime", "endtime", "creationtime", "updatetime"):
        if inc.get(k) and not _TIME.match(inc[k]):
            bad.append(f"{k} is not ISO 8601 with seconds and offset")
    if len(inc.get("description") or "") > 40:
        bad.append("description is over 40 characters")
    return bad


def to_xml(incidents: list[dict]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<incidents xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.gstatic.com/road-incidents/cifsv2.xsd">']
    for i in incidents:
        out.append(f"  <incident id={quoteattr(i['id'])}>")
        for k in ("creationtime", "updatetime", "type", "subtype", "description", "street", "direction", "polyline", "starttime", "endtime"):
            if i.get(k):
                out.append(f"    <{k}>{escape(str(i[k]))}</{k}>")
        out.append("  </incident>")
    out.append("</incidents>")
    return "\n".join(out)


# ---------------------------------------------------------------- build from the database

_ZONES = """
SELECT z.zone_id, z.event_type, z.cause, z.penalty_factor, z.bearing_center, z.valid_from, z.valid_to, z.created_at,
       z.confidence, z.origin, COALESCE(ST_Dimension(z.spine), -1) AS spine_dim
FROM geo.zone_of_avoidance z
WHERE z.city_id = %s AND z.status = 'ACTIVE' AND z.valid_to > now() AND z.valid_from <= now() + interval '7 days'
  AND z.confidence >= %s
ORDER BY z.zone_id
"""

# line spines: the stretch of each named road that runs along the spine, one line per street (a closure may cover
# only one street). A road counts when at least 45 m of it lies within 15 m of the spine; a side street that merely
# crosses contributes about 30 m and is left out. Short pieces of an already-chosen street fill the gaps.
_LINES = """
WITH z AS (SELECT zone_id, spine, ST_Buffer(spine::geography, 15)::geometry AS buf FROM geo.zone_of_avoidance WHERE zone_id = %s),
cut AS (
  SELECT e.name, e.road_gaz_id, e.length_m, ST_Intersection(e.geom, z.buf) AS part
  FROM z JOIN net.edge e ON e.geom && z.buf WHERE e.name IS NOT NULL AND ST_Intersects(e.geom, z.buf)),
len AS (SELECT *, ST_Length(part::geography) AS inside_m FROM cut),
main AS (SELECT DISTINCT name FROM len WHERE inside_m >= 45),
hit AS (SELECT l.* FROM len l JOIN main USING (name) WHERE l.inside_m >= 45 OR l.inside_m >= 0.8 * l.length_m),
grp AS (SELECT name, min(road_gaz_id) AS gaz, ST_LineMerge(ST_CollectionExtract(ST_Union(part), 2)) AS g FROM hit GROUP BY name),
parts AS (SELECT name, gaz, (ST_Dump(g)).geom AS line FROM grp)
SELECT name, gaz, row_number() OVER (PARTITION BY name ORDER BY ST_Length(line::geography) DESC, ST_AsText(ST_StartPoint(line))) AS part,
       ST_AsGeoJSON(ST_Segmentize(line::geography, 80)::geometry, 6)::json AS geom
FROM parts WHERE GeometryType(line) = 'LINESTRING' AND ST_Length(line::geography) >= 30
ORDER BY name, part
"""

# point, area or missing spine: the nearest named road to the zone, as a single point
_POINT = """
SELECT e.name, e.road_gaz_id AS gaz, ST_AsGeoJSON(ST_ClosestPoint(e.geom, p.pt), 6)::json AS geom
FROM (SELECT ST_PointOnSurface(COALESCE(spine, geom)) AS pt FROM geo.zone_of_avoidance WHERE zone_id = %s) p
JOIN net.edge e ON e.geom && ST_Expand(p.pt, 0.001)
WHERE e.name IS NOT NULL AND ST_DWithin(e.geom::geography, p.pt::geography, 80)
ORDER BY e.geom <-> p.pt LIMIT 1
"""


def build(c, city_id: int = CITY_ID, min_confidence: float = CIFS_MIN_CONFIDENCE) -> tuple[list[dict], list[dict]]:
    """-> (incidents, skipped). Skipped entries say which zone part was left out and why."""
    incidents: list[dict] = []
    skipped: list[dict] = []
    for z in c.execute(_ZONES, (city_id, min_confidence)).fetchall():
        base = {"creationtime": ts(z["created_at"]), "updatetime": ts(max(z["created_at"], z["valid_from"])),
                "description": DESCRIPTION.get(z["cause"], "Traffic restriction"),
                "starttime": ts(z["valid_from"]), "endtime": ts(z["valid_to"])}
        rows = c.execute(_LINES, (z["zone_id"],)).fetchall() if z["spine_dim"] == 1 else []
        made = 0
        for r in rows:
            coords = orient(r["geom"]["coordinates"], z["bearing_center"])
            t, sub = cifs_type(z["event_type"], z["cause"], float(z["penalty_factor"]), True)
            inc = {"id": f"dz{z['zone_id']}-r{r['gaz'] or 0}-{r['part']}", "type": t, **({"subtype": sub} if sub else {}), **base,
                   "street": r["name"], "polyline": polyline(coords),
                   "direction": "ONE_DIRECTION" if z["bearing_center"] is not None else "BOTH_DIRECTIONS"}
            problems = validate(inc)
            if problems:
                skipped.append({"zone_id": z["zone_id"], "street": r["name"], "why": problems})
            else:
                incidents.append(inc)
                made += 1
        if made:
            continue
        p = c.execute(_POINT, (z["zone_id"],)).fetchone()
        if p is None:
            skipped.append({"zone_id": z["zone_id"], "street": None, "why": ["no named road within 80 m of the zone"]})
            continue
        t, sub = cifs_type(z["event_type"], z["cause"], float(z["penalty_factor"]), False)       # a point is never a closure
        inc = {"id": f"dz{z['zone_id']}-p{p['gaz'] or 0}", "type": t, **({"subtype": sub} if sub else {}), **base,
               "street": p["name"], "polyline": polyline([p["geom"]["coordinates"]]), "direction": "BOTH_DIRECTIONS"}
        problems = validate(inc)
        if problems:
            skipped.append({"zone_id": z["zone_id"], "street": p["name"], "why": problems})
        else:
            incidents.append(inc)
    return incidents, skipped
