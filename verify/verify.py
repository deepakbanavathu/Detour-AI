#!/usr/bin/env python3
"""DetourAI verification: does the stack produce correct results, judged against things outside its own code.

Runs inside Docker on the compose network. Talks to the database directly (graph, matching, idempotency) and to
the API the way a client does (routes, advisories, latency). Writes one dated JSON report.

  verify.py run            [--skip-reimport] [--ci-random-pairs N]
  verify.py restart-before --report PATH        (then kill and start the worker from the host)
  verify.py restart-after  --report PATH
  verify.py osmose         --report PATH
  verify.py review-pack    --report PATH [--n 10]
  verify.py score-review   --report PATH --answers CSV --key JSON
  verify.py finalize       --report PATH          (prints the summary, sets the exit code)

Exit codes: 0 every criterion passed or is a recorded known issue, 1 at least one failed, 2 the script itself failed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(os.environ.get("DETOUR_ROOT", "/work"))
VERIFY = ROOT / "verify"
INPUTS = Path(os.environ.get("DETOUR_INPUTS", VERIFY / "inputs"))
API = os.environ.get("DETOUR_API", "http://api:8080").rstrip("/")
OSRM = os.environ.get("OSRM_URL", "").rstrip("/")
DB = os.environ.get("DATABASE_URL", "postgresql://detour:detour@db:5432/detour")
ADMIN = os.environ.get("ADMIN_TOKEN", "")
BITS = {"2W": 1, "3W": 2, "4W_HATCH": 4, "4W_SUV": 8, "LCV": 16, "HCV": 32, "BUS": 64}
CAR = "4W_HATCH"

# ------------------------------------------------------------------------------------------------ report


class Report:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self.crit = {c["id"]: c for c in json.loads((VERIFY / "criteria.json").read_text())["criteria"]}
        known = VERIFY / "known_issues.json"
        self.known = {k["id"]: k for k in json.loads(known.read_text())["issues"]} if known.exists() else {}
        self.d = data or {"started_at": now(), "results": {}, "details": {}, "meta": {}}

    @classmethod
    def load(cls, path: Path) -> "Report":
        return cls(path, json.loads(path.read_text()))

    def check(self, cid: str, value, diagnosis: str | None = None, evidence=None):
        c = self.crit[cid]
        if value is None:
            return self.skip(cid, "not measured")
        ok = {"==": value == c["threshold"], "<=": value <= c["threshold"], ">=": value >= c["threshold"]}[c["op"]]
        status = "pass" if ok else ("known_issue" if cid in self.known else "fail")
        r = {"group": c["group"], "metric": c["metric"], "value": round(value, 4) if isinstance(value, float) else value,
             "op": c["op"], "threshold": c["threshold"], "estimate": c["estimate"], "status": status}
        if not ok and diagnosis:
            r["likely_cause"] = diagnosis
        if not ok and cid in self.known:
            r["known_issue"] = self.known[cid]["summary"]
        if evidence is not None and not ok:
            r["evidence"] = evidence
        self.d["results"][cid] = r
        print(f"  {cid} {c['metric']:<42} {str(r['value']):>10} {c['op']} {c['threshold']:<8} {status.upper()}")

    def skip(self, cid: str, why: str):
        c = self.crit[cid]
        self.d["results"][cid] = {"group": c["group"], "metric": c["metric"], "status": "skip", "why": why}
        print(f"  {cid} {c['metric']:<42} {'':>10}    SKIP ({why})")

    def detail(self, key: str, value):
        self.d["details"][key] = value

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.d, indent=2, default=str))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    return psycopg.connect(DB, row_factory=dict_row, autocommit=True)


def one(c, sql, params=None):
    return c.execute(sql, params).fetchone()


def http() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=120)


# ------------------------------------------------------------------------------------------------ meta


def meta(c, rep: Report):
    m = {"api": API, "osrm": OSRM or None, "git_commit": os.environ.get("DETOUR_GIT", "unknown")}
    m["postgres"] = one(c, "SELECT version() AS v")["v"]
    for ext in ("postgis", "pgrouting", "h3", "h3_postgis", "timescaledb", "pg_stat_statements", "postgis_raster"):
        r = one(c, "SELECT extversion FROM pg_extension WHERE extname = %s", (ext,))
        m[ext] = r["extversion"] if r else None
    m["counts"] = one(c, """SELECT (SELECT count(*) FROM net.edge) edges, (SELECT count(*) FROM net.node) nodes,
                            (SELECT count(*) FROM geo.gazetteer) places, (SELECT count(*) FROM osm.highway) osm_ways""")
    ext = one(c, "SELECT ST_XMin(e) x0, ST_YMin(e) y0, ST_XMax(e) x1, ST_YMax(e) y1 FROM (SELECT ST_Extent(geom)::geometry e FROM net.node) q")
    m["extent"] = [ext["x0"], ext["y0"], ext["x1"], ext["y1"]]
    files = {}
    if INPUTS.exists():
        for f in sorted(INPUTS.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                files[str(f.relative_to(INPUTS))] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    m["inputs"] = files
    rep.d["meta"] = m


# ------------------------------------------------------------------------------------------------ graph

_BORDER = "ST_Distance(nn.geom::geography, ST_Boundary(x.g)::geography) > 500"


def graph(c, rep: Report):
    print("\n[graph] integrity of net.* built from OSM")
    total = one(c, "SELECT count(*) n FROM net.edge")["n"]
    if not total:
        for cid in [k for k, v in rep.crit.items() if v["group"] in ("graph", "vehicle")]:
            rep.skip(cid, "net.edge is empty: run the import first")
        return
    r = one(c, """
        SELECT count(*) FILTER (WHERE NOT ST_IsValid(e.geom)) invalid,
               count(*) FILTER (WHERE ST_NPoints(e.geom) < 2) short_pts,
               count(*) FILTER (WHERE abs(e.length_m - ST_Length(e.geom::geography)) > 1) len_mismatch,
               count(*) FILTER (WHERE e.length_m < 0.5) tiny
        FROM net.edge e""")
    rep.check("G01", r["invalid"], "import: line built from out-of-order vertices; check osm.way_vertex for the way ids")
    rep.check("G02", r["short_pts"], "import: segment split produced a single vertex")
    rep.check("G03", r["len_mismatch"], "import: length computed before reversal or in degrees")
    rep.check("G04", one(c, """
        SELECT count(*) n FROM net.edge e JOIN net.node s ON s.node_id = e.src JOIN net.node d ON d.node_id = e.dst
        WHERE ST_Distance(ST_StartPoint(e.geom)::geography, s.geom::geography) > 0.5
           OR ST_Distance(ST_EndPoint(e.geom)::geography, d.geom::geography) > 0.5""")["n"],
        "import: oneway=-1 reversal swapped src/dst without reversing the line, or the reverse")
    dup = [dict(r) for r in c.execute("""SELECT src, dst, array_agg(DISTINCT osm_way_id) ways, array_agg(DISTINCT name) names FROM net.edge
                                          GROUP BY src, dst, ST_AsBinary(geom) HAVING count(*) > 1 LIMIT 20""").fetchall()]
    rep.check("G05", len(dup), ("OSM data: two different ways drawn over the same road (e.g. a tunnel way on top of the street); fix in OSM"
                                if dup and all(len(d["ways"]) > 1 for d in dup) else "import: the same OSM way loaded twice"), dup)
    rep.check("G06", r["tiny"] / total, "OSM data: duplicated nodes a few centimetres apart")
    rep.check("G07", one(c, """SELECT count(*) n FROM net.edge e LEFT JOIN net.edge_dim d USING (edge_id) LEFT JOIN net.edge_dynamic y USING (edge_id)
                               WHERE d.edge_id IS NULL OR y.edge_id IS NULL""")["n"], "import: build_network.sql stopped part way; rerun it")
    rep.check("G08", one(c, """SELECT count(*) n FROM net.edge e JOIN osm.highway h ON h.way_id = e.osm_way_id
                               WHERE h.tags ? 'name' AND e.road_gaz_id IS NULL""")["n"],
              "import: build_gazetteer.sql not run after build_network.sql (a network rebuild clears road links)")
    s = one(c, "SELECT (SELECT count(*) FROM osm.highway) w, (SELECT count(*) FROM osm.highway h WHERE NOT EXISTS (SELECT 1 FROM osm.way_ok o WHERE o.way_id = h.way_id)) skipped")
    rep.check("G09", s["skipped"] / max(1, s["w"]), "import: ways whose vertex count differs from node count (osm2pgsql version or a way crossing the extract edge)")

    def largest_share(where: str) -> tuple[float, int]:
        q = f"""
        WITH cc AS (SELECT node, component FROM pgr_connectedComponents(
               $$SELECT edge_id AS id, src AS source, dst AS target, 1::float8 AS cost, 1::float8 AS reverse_cost FROM net.edge e {where}$$)),
        el AS (SELECT cc.component, sum(e.length_m) len FROM net.edge e JOIN cc ON cc.node = e.src {where.replace('WHERE', 'WHERE true AND') if where else ''} GROUP BY 1)
        SELECT max(len) / NULLIF(sum(len), 0) share, count(*) n FROM el"""
        r = one(c, q)
        return float(r["share"] or 0), int(r["n"])

    sh, n = largest_share("WHERE e.highway_rank <= 4")
    rep.check("G10", sh, "OSM data if the islands are real gaps (see Osmose); import if they sit at way splits", {"components": n})
    sh, n = largest_share("")
    rep.check("G11", sh, "OSM data: private campuses and unconnected service roads", {"components": n})
    scc = one(c, """
        WITH sc AS (SELECT node, component FROM pgr_strongComponents($$
               SELECT e.edge_id AS id, e.src AS source, e.dst AS target,
                      CASE WHEN d.static_mask & 4 > 0 THEN 1 ELSE -1 END::float8 AS cost,
                      CASE WHEN d.static_mask & 4 > 0 AND NOT e.oneway THEN 1 ELSE -1 END::float8 AS reverse_cost
               FROM net.edge e JOIN net.edge_dim d USING (edge_id)$$)),
        el AS (SELECT a.component, sum(e.length_m) len FROM net.edge e JOIN net.edge_dim d USING (edge_id)
               JOIN sc a ON a.node = e.src JOIN sc b ON b.node = e.dst AND b.component = a.component
               WHERE d.static_mask & 4 > 0 GROUP BY 1),
        tot AS (SELECT sum(e.length_m) t FROM net.edge e JOIN net.edge_dim d USING (edge_id) WHERE d.static_mask & 4 > 0)
        SELECT (SELECT max(len) FROM el) / NULLIF((SELECT t FROM tot), 0) share""")
    rep.check("G12", float(scc["share"] or 0), "OSM data (one-way tagged the wrong way) or import (oneway=-1 handling); G04 separates them")
    de = one(c, f"""
        WITH deg AS (SELECT n, count(*) k FROM (SELECT src n FROM net.edge UNION ALL SELECT dst FROM net.edge) q GROUP BY n),
        art AS (SELECT DISTINCT n FROM (SELECT src n FROM net.edge WHERE highway_rank <= 4 UNION SELECT dst FROM net.edge WHERE highway_rank <= 4) q),
        x AS (SELECT ST_Extent(geom)::geometry g FROM net.node)
        SELECT count(*) FILTER (WHERE deg.k = 1) dead, count(*) total
        FROM art JOIN deg USING (n) JOIN net.node nn ON nn.node_id = art.n, x WHERE {_BORDER}""")
    rep.check("G13", de["dead"] / max(1, de["total"]), "OSM data: arterial ends short of a junction (check Osmose item 1xxx near it)",
              {"dead_ends": de["dead"], "arterial_nodes": de["total"]})
    tr = one(c, f"""
        WITH arcs AS (SELECT e.src a, e.dst b FROM net.edge e JOIN net.edge_dim d USING (edge_id) WHERE d.static_mask & 4 > 0
                      UNION ALL SELECT e.dst, e.src FROM net.edge e JOIN net.edge_dim d USING (edge_id) WHERE d.static_mask & 4 > 0 AND NOT e.oneway),
        ins AS (SELECT DISTINCT b n FROM arcs), outs AS (SELECT DISTINCT a n FROM arcs),
        x AS (SELECT ST_Extent(geom)::geometry g FROM net.node)
        SELECT count(*) FILTER (WHERE outs.n IS NULL) traps, count(*) total
        FROM ins LEFT JOIN outs USING (n) JOIN net.node nn ON nn.node_id = ins.n, x WHERE {_BORDER}""")
    trap_examples = [dict(r) for r in c.execute(f"""
        WITH arcs AS (SELECT e.src a, e.dst b FROM net.edge e JOIN net.edge_dim d USING (edge_id) WHERE d.static_mask & 4 > 0
                      UNION ALL SELECT e.dst, e.src FROM net.edge e JOIN net.edge_dim d USING (edge_id) WHERE d.static_mask & 4 > 0 AND NOT e.oneway),
        x AS (SELECT ST_Extent(geom)::geometry g FROM net.node)
        SELECT nn.node_id, ST_Y(nn.geom) lat, ST_X(nn.geom) lng FROM (SELECT DISTINCT b n FROM arcs) ins
        LEFT JOIN (SELECT DISTINCT a n FROM arcs) outs USING (n) JOIN net.node nn ON nn.node_id = ins.n, x
        WHERE outs.n IS NULL AND {_BORDER} LIMIT 10""").fetchall()]
    rep.check("G14", tr["traps"] / max(1, tr["total"]), "OSM data (one-way into a dead end, a missing turn link) or routing weights (an edge excluded for cars by an estimated width)",
              {"traps": tr["traps"], "car_nodes": tr["total"], "examples": trap_examples})


def vehicle(c, rep: Report):
    print("\n[vehicle] who may use which edge")
    if not one(c, "SELECT count(*) n FROM net.edge")["n"]:
        return
    # size only: where OSM access rules allow both, a vehicle that fits must not be refused while a bigger one passes
    has_access = bool(one(c, "SELECT 1 x FROM information_schema.columns WHERE table_schema = 'net' AND table_name = 'edge_dim' AND column_name = 'access_mask'"))
    acc = (lambda bits: f" AND access_mask & {bits} = {bits}") if has_access else (lambda bits: "")
    r = one(c, f"""SELECT count(*) FILTER (WHERE static_mask & 4 > 0 AND static_mask & 1 = 0{acc(5)}) car_not_2w,
                          count(*) FILTER (WHERE static_mask & 32 > 0 AND static_mask & 4 = 0{acc(36)}) hcv_not_car FROM net.edge_dim""")
    rep.check("V01", r["car_not_2w"], "import: net.static_mask rule (a narrower vehicle failing where a wider one passes)")
    rep.check("V02", r["hcv_not_car"], "import: net.static_mask rule")
    # OSM access, read here independently of the import: the most specific key present decides; only 'no' closes
    k = lambda *keys: "lower(COALESCE(" + ", ".join(f"NULLIF(btrim(t.tags->>'{x}'), '')" for x in keys) + "))"   # noqa: E731
    moto, car, hgv = (f"{k('motorcycle', 'motor_vehicle', 'vehicle', 'access')} = 'no' AND d.static_mask & 1 > 0",
                      f"{k('motorcar', 'motor_vehicle', 'vehicle', 'access')} = 'no' AND d.static_mask & 4 > 0",
                      f"{k('hgv', 'motor_vehicle', 'vehicle', 'access')} = 'no' AND d.static_mask & 32 > 0")
    a = one(c, f"""SELECT count(*) FILTER (WHERE {moto}) moto, count(*) FILTER (WHERE {car}) car, count(*) FILTER (WHERE {hgv}) hgv
                   FROM net.edge e JOIN net.edge_dim d USING (edge_id) JOIN osm.highway t ON t.way_id = e.osm_way_id""")
    note = ("import: the access tags are not folded into net.edge_dim.static_mask (net.access_mask), or osm.highway lacks the "
            "vehicle and motor_vehicle keys because the data was imported with an older detour.lua: re-import")
    rep.check("V03", a["moto"], note, _access_examples(c, moto))
    rep.check("V04", a["car"], note, _access_examples(c, car))
    rep.check("V05", a["hgv"], note, _access_examples(c, hgv))


def _access_examples(c, where: str) -> list[dict]:
    return [dict(r) for r in c.execute(f"""SELECT DISTINCT e.osm_way_id, e.name, e.highway FROM net.edge e JOIN net.edge_dim d USING (edge_id)
                                           JOIN osm.highway t ON t.way_id = e.osm_way_id WHERE {where} LIMIT 5""").fetchall()]


# ------------------------------------------------------------------------------------------------ pairs, OSRM, GPX overlap


def read_pairs() -> list[dict]:
    f = INPUTS / "pairs.csv"
    if not f.exists():
        return []
    out = []
    with f.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("id") or "").strip() or row["id"].startswith("#"):
                continue
            row["vehicles"] = {"2w": ["2W"], "car": [CAR], "both": ["2W", CAR]}[row["vehicle"].strip().lower()]
            row["from"] = f"{float(row['from_lat']):.6f},{float(row['from_lng']):.6f}"
            row["to"] = f"{float(row['to_lat']):.6f},{float(row['to_lng']):.6f}"
            out.append(row)
    return out


def random_pairs(c, n: int, seed: int = 7) -> list[dict]:
    """CI: node pairs 2 to 8 km apart on arterials, from a fixed seed."""
    nodes = [dict(r) for r in c.execute("""SELECT DISTINCT n.node_id, ST_Y(n.geom) lat, ST_X(n.geom) lng FROM net.node n
                                           JOIN net.edge e ON n.node_id IN (e.src, e.dst) WHERE e.highway_rank <= 3 ORDER BY 1""").fetchall()]
    rnd, out, tries = random.Random(seed), [], 0
    while len(out) < n and tries < 5000 and len(nodes) > 1:
        tries += 1
        a, b = rnd.sample(nodes, 2)
        d = _dist(a["lat"], a["lng"], b["lat"], b["lng"])
        if 2000 <= d <= 8000:
            out.append({"id": f"ci{len(out) + 1}", "from": f"{a['lat']:.6f},{a['lng']:.6f}", "to": f"{b['lat']:.6f},{b['lng']:.6f}",
                        "vehicles": ["2W", CAR], "usual_minutes": "", "usual_via": "", "gpx": ""})
    return out


def _dist(lat1, lng1, lat2, lng2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2)) * 111320
    return math.hypot((lng2 - lng1) * k, (lat2 - lat1) * 110540)


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = re.sub(r"\brd\b", "road", s)
    return re.sub(r"\s+", " ", s).strip()


def osrm_route(frm: str, to: str) -> dict | None:
    if not OSRM:
        return None
    (a, b), (cc, d) = (map(float, frm.split(",")), map(float, to.split(",")))
    try:
        r = httpx.get(f"{OSRM}/route/v1/driving/{b},{a};{d},{cc}", params={"overview": "full", "geometries": "geojson"}, timeout=60)
        j = r.json()
        if j.get("code") != "Ok":
            return {"error": j.get("code")}
        rt = j["routes"][0]
        return {"distance_m": rt["distance"], "duration_s": rt["duration"], "geometry": rt["geometry"]}
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e)[:200]}


def explain_osrm_gap(c, osrm_geom: dict) -> dict:
    """Where OSRM drove and DetourAI's car may not: tells weights/import apart from a speed-model difference."""
    r = one(c, """
        WITH g AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) g),
        pts AS (SELECT (ST_DumpPoints(ST_Segmentize(g::geography, 50)::geometry)).geom p FROM g),
        m AS (SELECT h.* FROM pts LEFT JOIN LATERAL (
                SELECT d.static_mask, d.width_src, y.gate_state FROM net.edge e JOIN net.edge_dim d USING (edge_id) JOIN net.edge_dynamic y USING (edge_id)
                WHERE e.geom && ST_Expand(pts.p, 0.0004) AND ST_DWithin(e.geom::geography, pts.p::geography, 25)
                ORDER BY e.geom <-> pts.p LIMIT 1) h ON true)
        SELECT count(*) n,
               count(*) FILTER (WHERE static_mask IS NULL) no_edge,
               count(*) FILTER (WHERE static_mask & 4 = 0) car_excluded,
               count(*) FILTER (WHERE static_mask & 4 = 0 AND width_src IN ('CLASS_DEFAULT','LANES_INFER')) car_excluded_estimated_width,
               count(*) FILTER (WHERE gate_state IS NOT NULL AND gate_state <> 'OPEN') at_gate
        FROM m""", (json.dumps(osrm_geom),))
    n = max(1, r["n"])
    if r["no_edge"] / n > 0.05:
        cause = "import: OSRM drove on roads absent from net.edge (a filtered highway type or a way skipped by the import)"
    elif r["car_excluded"] / n > 0.05:
        cause = ("routing weights: DetourAI excludes cars from roads OSRM uses; " +
                 ("their width is estimated from road class, so measure them (OSM data lacks width=)" if r["car_excluded_estimated_width"] else "check the static mask rule"))
    elif r["at_gate"]:
        cause = "routing weights: the unknown-gate cost pushes DetourAI around a barrier OSRM ignores"
    else:
        cause = "routing weights: both use the same roads; the speed models differ (DetourAI uses class defaults and maxspeed)"
    return {"sampled_points": r["n"], "osrm_points_not_on_detour_graph": r["no_edge"], "osrm_points_car_excluded": r["car_excluded"],
            "of_which_estimated_width": r["car_excluded_estimated_width"], "osrm_points_at_gates": r["at_gate"], "likely_cause": cause}


def read_gpx(path: Path) -> list[tuple[float, float]]:
    pts = []
    for el in ET.parse(path).getroot().iter():
        if el.tag.endswith("trkpt") or el.tag.endswith("rtept"):
            pts.append((float(el.get("lat")), float(el.get("lon"))))
    return pts


def gpx_line(pts: list[tuple[float, float]]) -> dict:
    return {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in pts]}


def pairs(c, rep: Report, ci_random: int = 0):
    print("\n[routing] known pairs")
    ps = random_pairs(c, ci_random) if ci_random else read_pairs()
    if not ps:
        for cid in ("R01", "R02", "R03", "R04", "R05", "R06", "R07", "V06"):
            rep.skip(cid, "no pairs: fill verify/inputs/pairs.csv")
        return []
    rows, snaps, times = [], [], {}
    with http() as h:
        for p in ps:
            for v in p["vehicles"]:
                t0 = time.perf_counter()
                r = h.get("/v1/route", params={"profile": v, "from": p["from"], "to": p["to"]})
                ms = (time.perf_counter() - t0) * 1000
                row = {"pair": p["id"], "vehicle": v, "status": r.status_code, "ms": round(ms)}
                if r.status_code == 200:
                    j = r.json()
                    row.update(distance_m=j["distance_m"], duration_s=j["duration_s"], snap_from=j["snap_from"]["dist_m"], snap_to=j["snap_to"]["dist_m"],
                               roads=sorted({e["name"] for e in j["edges"] if e.get("name")}), geometry=j["geometry"])
                    snaps += [j["snap_from"]["dist_m"], j["snap_to"]["dist_m"]]
                    times[(p["id"], v)] = j["duration_s"]
                else:
                    row["error"] = r.text[:200]
                rows.append(row)
    found = sum(1 for r in rows if r["status"] == 200)
    rep.check("R01", found / len(rows), "see the failing rows: a 404 'no road open' is the vehicle mask near the pin; 'no path' is connectivity (G10 to G14)",
              [{k: r[k] for k in ("pair", "vehicle", "status", "error")} for r in rows if r["status"] != 200])
    far = []
    for r in rows:
        for end, key in (("from", "snap_from"), ("to", "snap_to")):
            if r.get(key, 0) > 150:
                p = next(x for x in ps if x["id"] == r["pair"])
                lat, lng = map(float, p[end].split(","))
                n = one(c, """SELECT e.name, e.highway, d.static_mask, d.access_mask, ST_Distance(e.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) dist
                              FROM net.edge e JOIN net.edge_dim d USING (edge_id) ORDER BY e.geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) LIMIT 1""", (lng, lat, lng, lat))
                cause = ("OSM data or import: no road at all near the pin" if not n or n["dist"] > 150 else
                         "vehicle rules: the nearest road is closed to this vehicle by an OSM access tag" if not n["access_mask"] & BITS[r["vehicle"]] else
                         "vehicle rules: the nearest road is too narrow (or low) for this vehicle; check its width")
                far.append({"pair": r["pair"], "vehicle": r["vehicle"], "end": end, "snap_m": r[key], "nearest_any_road": n and {"name": n["name"], "dist_m": round(n["dist"])}, "likely_cause": cause})
    rep.check("R02", sum(1 for s in snaps if s <= 150) / max(1, len(snaps)), "see each pin's likely_cause", far)

    # OSRM differential, car only (OSRM has no two-wheeler profile)
    diffs, gaps = [], []
    if OSRM:
        for p in ps:
            mine = next((r for r in rows if r["pair"] == p["id"] and r["vehicle"] == CAR and r["status"] == 200), None)
            if not mine:
                continue
            o = osrm_route(p["from"], p["to"])
            if not o or "error" in o:
                gaps.append({"pair": p["id"], "osrm_error": (o or {}).get("error")})
                continue
            ratio = mine["distance_m"] / max(1.0, o["distance_m"])
            d = {"pair": p["id"], "detour_m": round(mine["distance_m"]), "osrm_m": round(o["distance_m"]), "ratio": round(ratio, 3)}
            if abs(ratio - 1) > 0.15:
                d.update(explain_osrm_gap(c, o["geometry"]))
            diffs.append(d)
        if diffs:
            rep.check("R03", sum(1 for d in diffs if abs(d["ratio"] - 1) <= 0.15) / len(diffs), "see evidence: each gap carries its likely cause",
                      [d for d in diffs if abs(d["ratio"] - 1) > 0.15])
            rep.check("R04", sum(1 for d in diffs if abs(d["ratio"] - 1) > 0.5), "see evidence", [d for d in diffs if abs(d["ratio"] - 1) > 0.5])
        else:
            rep.skip("R03", "OSRM returned no routes")
            rep.skip("R04", "OSRM returned no routes")
    else:
        rep.skip("R03", "OSRM_URL not set")
        rep.skip("R04", "OSRM_URL not set")

    # against what you told it
    via_scores, ratio_ok, rated = [], 0, 0
    for p in ps:
        names = [_norm(x) for x in (p.get("usual_via") or "").split(";") if x.strip()]
        for v in p["vehicles"]:
            mine = next((r for r in rows if r["pair"] == p["id"] and r["vehicle"] == v and r["status"] == 200), None)
            if not mine:
                continue
            if names:
                got = [_norm(x) for x in mine["roads"]]
                hit = sum(1 for n in names if any(n in g or g in n for g in got))
                via_scores.append({"pair": p["id"], "vehicle": v, "named": len(names), "found": hit, "missing": [n for n in names if not any(n in g or g in n for g in got)]})
            if (p.get("usual_minutes") or "").strip():
                rated += 1
                ratio = mine["duration_s"] / 60 / float(p["usual_minutes"])
                ratio_ok += 0.35 <= ratio <= 1.2
                mine["time_ratio"] = round(ratio, 2)
    if via_scores:
        rep.check("R05", sum(1 for s in via_scores if s["found"] / s["named"] >= 0.5) / len(via_scores),
                  "routing weights if the named roads exist in the graph but are avoided; gazetteer or OSM names if a named road is spelled differently in OSM",
                  [s for s in via_scores if s["found"] / s["named"] < 0.5])
    else:
        rep.skip("R05", "no usual_via in pairs.csv")
    rep.check("R06", ratio_ok / rated if rated else None, "routing weights: free-flow speeds by road class",
              [(r["pair"], r["vehicle"], r.get("time_ratio")) for r in rows if r.get("time_ratio") and not 0.35 <= r["time_ratio"] <= 1.2])

    # overlap with what you actually drove
    overlaps = []
    for p in ps:
        g = (p.get("gpx") or "").strip()
        if not g or not (INPUTS / "gpx" / g).exists():
            continue
        line = gpx_line(read_gpx(INPUTS / "gpx" / g))
        for v in p["vehicles"]:
            mine = next((r for r in rows if r["pair"] == p["id"] and r["vehicle"] == v and r["status"] == 200), None)
            if mine:
                o = one(c, """SELECT ST_Length(ST_Intersection(r, ST_Buffer(t::geography, 30)::geometry)::geography) / NULLIF(ST_Length(r::geography), 0) s
                              FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) r, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) t) q""",
                        (json.dumps(mine["geometry"]), json.dumps(line)))["s"]
                overlaps.append({"pair": p["id"], "vehicle": v, "overlap": round(float(o or 0), 3)})
    rep.check("R07", sum(1 for o in overlaps if o["overlap"] >= 0.7) / len(overlaps) if overlaps else None,
              "routing weights when your road is in the graph and allowed (check T02/T03 for the same trip); OSM data when it is not",
              [o for o in overlaps if o["overlap"] < 0.7])

    # 2W against car
    over = [{"pair": p["id"], "2w_s": times[(p["id"], "2W")], "car_s": times[(p["id"], CAR)]}
            for p in ps if (p["id"], "2W") in times and (p["id"], CAR) in times and times[(p["id"], "2W")] > 1.15 * times[(p["id"], CAR)]]
    both = sum(1 for p in ps if (p["id"], "2W") in times and (p["id"], CAR) in times)
    rep.check("V06", len(over) if both else None, "routing weights: a two-wheeler excluded from a road the car uses (V01, V03) or the 70 km/h cap on a long fast road", over)
    for r in rows:
        r.pop("geometry", None)
    rep.detail("pairs", rows)
    rep.detail("osrm", diffs + gaps)
    rep.detail("via", via_scores)
    rep.detail("gpx_overlap", overlaps)
    return [r["ms"] for r in rows if r["status"] == 200]


# ------------------------------------------------------------------------------------------------ gates


def gates(c, rep: Report):
    print("\n[gates] tape measurements against the mask")
    f = INPUTS / "gates.csv"
    rows = []
    if f.exists():
        with f.open(newline="", encoding="utf-8-sig") as fh:
            rows = [r for r in csv.DictReader(fh) if (r.get("id") or "").strip() and not r["id"].startswith("#")]
    if not rows:
        rep.skip("V07", "no gates: fill verify/inputs/gates.csv")
        rep.skip("V08", "no gates")
        return
    disagree, through, out = [], [], []
    with http() as h:
        for g in rows:
            lat, lng, w = float(g["lat"]), float(g["lng"]), float(g["width_m"])
            scoot, car = g["scooter_passes"].strip().lower() == "yes", g["car_passes"].strip().lower() == "yes"
            edges = [dict(r) for r in c.execute("""
                SELECT e.edge_id, e.name, e.oneway, d.width_m, d.width_src, d.static_mask, d.clearance_m, d.max_gvw_t, d.layout_private,
                       net.static_mask(e.oneway, %s::real, d.clearance_m, d.max_gvw_t, d.layout_private) AS measured_mask,
                       ST_Distance(e.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) dist
                FROM net.edge e JOIN net.edge_dim d USING (edge_id)
                WHERE ST_DWithin(e.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 20)
                ORDER BY dist LIMIT 4""", (w, lng, lat, lng, lat)).fetchall()]
            item = {"gate": g["id"], "width_m": w, "scooter_passes": scoot, "car_passes": car, "edges": edges}
            if not edges:
                item["problem"] = "no edge within 20 m: the lane is missing from OSM or filtered by the import"
                disagree.append(item)
            else:
                e = edges[0]
                m = e["measured_mask"]
                if bool(m & 1) != scoot or bool(m & 4) != car:
                    reason = ("import: the mask rule requires room for an oncoming car beside you on two-way edges "
                              f"(scooter needs {0.8 + 2.2:.1f} m on a two-way edge, {0.8 + 0.4:.1f} m on a one-way)") if not e["oneway"] else "import: mask rule"
                    item["problem"] = f"with width {w} m the mask says 2W={bool(m & 1)}, car={bool(m & 4)}; observed 2W={scoot}, car={car}. {reason}"
                    disagree.append(item)
                if not car and g.get("a_lat"):
                    r = h.get("/v1/route", params={"profile": CAR, "from": f"{g['a_lat']},{g['a_lng']}", "to": f"{g['b_lat']},{g['b_lng']}"})
                    if r.status_code == 200 and any(x["edge_id"] in {ed["edge_id"] for ed in edges[:2]} for x in r.json()["edges"]):
                        through.append({"gate": g["id"], "current_width_m": e["width_m"], "width_src": e["width_src"],
                                        "likely_cause": "OSM data: no width= on the lane, so the class default lets a car through; measured width is not yet in net.edge_dim"})
            out.append(item)
    rep.check("V07", len(disagree), "see each gate's problem text", disagree)
    rep.check("V08", len(through), "see evidence", through)
    rep.detail("gates", out)


# ------------------------------------------------------------------------------------------------ GPX map matching


def gpx_tracks(c, rep: Report):
    print("\n[gpx] recorded trips matched onto the graph")
    folder = INPUTS / "gpx"
    files = sorted(folder.glob("*.gpx")) if folder.exists() else []
    if not files:
        for cid in ("T01", "T02", "T03", "T04"):
            rep.skip(cid, "no GPX files in verify/inputs/gpx")
        return
    c.execute("CREATE TEMP TABLE IF NOT EXISTS vpts (trip text, vehicle text, seq int, geom geometry(Point,4326), heading real)")
    c.execute("TRUNCATE vpts")
    trips = []
    for f in files:
        m = re.search(r"_(2w|car|suv)\.gpx$", f.name, re.I)
        veh = {"2w": "2W", "car": CAR, "suv": "4W_SUV"}[m.group(1).lower()] if m else CAR
        pts = read_gpx(f)
        kept, last = [], None
        for lat, lng in pts:                                     # resample: one point every 15 m or more
            if last is None or _dist(last[0], last[1], lat, lng) >= 15:
                hd = None if last is None else (math.degrees(math.atan2((lng - last[1]) * math.cos(math.radians(lat)), lat - last[0])) + 360) % 360
                kept.append((lat, lng, hd))
                last = (lat, lng)
        with c.cursor().copy("COPY vpts (trip, vehicle, seq, geom, heading) FROM STDIN") as cp:
            for i, (lat, lng, hd) in enumerate(kept):
                cp.write_row((f.name, veh, i, f"SRID=4326;POINT({lng} {lat})", hd))
        trips.append((f.name, veh, len(pts), len(kept)))
    res = [dict(r) for r in c.execute("""
        WITH m AS (
          SELECT p.trip, p.vehicle, p.seq, p.heading, p.geom, h.*
          FROM vpts p LEFT JOIN LATERAL (
            SELECT e.edge_id, e.oneway, e.bearing_deg, d.static_mask, e.name,
                   ST_Distance(e.geom::geography, p.geom::geography) dist,
                   CASE WHEN p.heading IS NULL THEN 0
                        ELSE abs(mod((p.heading - e.bearing_deg + 540)::numeric, 360) - 180) END AS fwd_diff
            FROM net.edge e JOIN net.edge_dim d USING (edge_id)
            WHERE e.geom && ST_Expand(p.geom, 0.0003) AND ST_DWithin(e.geom::geography, p.geom::geography, 25)
            ORDER BY CASE WHEN p.heading IS NULL THEN 0
                          ELSE LEAST(abs(mod((p.heading - e.bearing_deg + 540)::numeric, 360) - 180),
                                     abs(mod((p.heading - e.bearing_deg + 360)::numeric, 360) - 180)) END > 45,
                     ST_Distance(e.geom::geography, p.geom::geography)
            LIMIT 1) h ON true)
        SELECT trip, vehicle, count(*) n,
               count(*) FILTER (WHERE edge_id IS NOT NULL) matched,
               count(*) FILTER (WHERE edge_id IS NOT NULL AND static_mask & CASE vehicle WHEN '2W' THEN 1 WHEN '4W_SUV' THEN 8 ELSE 4 END > 0) allowed,
               count(*) FILTER (WHERE edge_id IS NOT NULL AND oneway AND fwd_diff > 135) against_oneway
        FROM m GROUP BY trip, vehicle ORDER BY trip""").fetchall()]
    unmatched = [dict(r) for r in c.execute("""
        SELECT p.trip, ST_Y(p.geom) lat, ST_X(p.geom) lng,
               EXISTS (SELECT 1 FROM osm.highway h WHERE h.geom && ST_Expand(p.geom, 0.0003) AND ST_DWithin(h.geom::geography, p.geom::geography, 25)) AS in_osm_tables
        FROM vpts p WHERE NOT EXISTS (SELECT 1 FROM net.edge e WHERE e.geom && ST_Expand(p.geom, 0.0003) AND ST_DWithin(e.geom::geography, p.geom::geography, 25))
        ORDER BY p.trip, p.seq LIMIT 60""").fetchall()]
    for u in unmatched:
        u["likely_cause"] = ("import: the way is loaded in osm.highway but has no edge (skipped by the split or vertex check)" if u["in_osm_tables"]
                             else "OSM data or import filter: no loaded way here; check the raw .pbf with osmium (see VERIFY.md)")
    disallowed = [dict(r) for r in c.execute("""
        SELECT p.trip, p.vehicle, e.edge_id, e.name, e.highway, d.width_m, d.width_src, ST_Y(p.geom) lat, ST_X(p.geom) lng
        FROM vpts p JOIN LATERAL (SELECT e.* FROM net.edge e WHERE e.geom && ST_Expand(p.geom, 0.0003)
                                  AND ST_DWithin(e.geom::geography, p.geom::geography, 25) ORDER BY e.geom <-> p.geom LIMIT 1) e ON true
        JOIN net.edge_dim d USING (edge_id)
        WHERE d.static_mask & CASE p.vehicle WHEN '2W' THEN 1 WHEN '4W_SUV' THEN 8 ELSE 4 END = 0 LIMIT 40""").fetchall()]
    for dsl in disallowed:
        dsl["likely_cause"] = ("OSM data: no width= tag, so the width is a class default; measure it" if dsl["width_src"] in ("CLASS_DEFAULT", "LANES_INFER")
                               else "import: the static mask rule rejects a mapped width this vehicle actually used")
    tot = sum(r["n"] for r in res)
    rep.check("T01", sum(r["matched"] for r in res) / max(1, tot), "see unmatched points: each says import or OSM data", unmatched[:20])
    car = [r for r in res if r["vehicle"] != "2W"]
    two = [r for r in res if r["vehicle"] == "2W"]
    rep.check("T02", sum(r["allowed"] for r in car) / max(1, sum(r["matched"] for r in car)) if car else None,
              "see evidence: each edge a car used but the graph forbids", [d for d in disallowed if d["vehicle"] != "2W"][:20])
    rep.check("T03", sum(r["allowed"] for r in two) / max(1, sum(r["matched"] for r in two)) if two else None,
              "see evidence", [d for d in disallowed if d["vehicle"] == "2W"][:20])
    rep.check("T04", sum(r["against_oneway"] for r in car) / max(1, sum(r["matched"] for r in car)) if car else None,
              "if G04 passes: OSM data (one-way tagged the wrong way, or two-way in reality); if G04 fails: import",
              [r for r in car if r["against_oneway"]])
    rep.detail("gpx", {"trips": [{"file": t, "vehicle": v, "points": n, "resampled": k} for t, v, n, k in trips], "per_trip": res,
                       "two_wheeler_against_oneway_note": "reported only; riding against one-ways is common and not a graph error"})


# ------------------------------------------------------------------------------------------------ advisories


def advisories(c, rep: Report):
    print("\n[advisory] your labelled advisories against the pipeline (dry run, nothing is stored)")
    f = INPUTS / "advisories.geojson"
    feats = json.loads(f.read_text(encoding="utf-8")).get("features", []) if f.exists() else []
    feats = [x for x in feats if (x.get("properties") or {}).get("text")]
    if not feats:
        for cid in ("A01", "A02", "A03", "A04", "A05", "A06"):
            rep.skip(cid, "no advisories: fill verify/inputs/advisories.geojson")
        return
    srid = one(c, "SELECT utm_srid FROM geo.city WHERE city_id = 1")["utm_srid"]
    rows = []
    with http() as h:
        for ft in feats:
            p = ft["properties"]
            body = {"text": p["text"], "commit": False, "source_handle": p.get("source") or "@verify"}
            if p.get("posted_at"):
                body["ts_published"] = p["posted_at"]
            r = h.post("/v1/advisories/paste", json=body)
            d = r.json() if r.status_code == 200 else {"ok": False, "reason": f"HTTP {r.status_code}: {r.text[:120]}"}
            row = {"id": p.get("id"), "expect": p.get("expect", "zone"), "ok": d.get("ok"), "reason": d.get("reason"),
                   "event": (d.get("ir") or {}).get("event_type"), "anchors": [a["canonical"] for a in d.get("anchors", [])],
                   "candidates": [x.get("canonical") for x in d.get("candidates") or []][:3]}
            zg = (d.get("zone") or {}).get("geometry")
            placed = bool(zg and zg.get("coordinates"))
            row["placed"] = placed
            if placed and ft.get("geometry"):
                m = one(c, f"""
                    WITH z AS (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), {srid}) g),
                         l AS (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), {srid}) g)
                    SELECT ST_Length(ST_Intersection(l.g, z.g)) / NULLIF(ST_Length(l.g), 0) coverage,
                           ST_Area(ST_Difference(z.g, ST_Buffer(l.g, 150))) / NULLIF(ST_Area(z.g), 0) spill
                    FROM z, l""", (json.dumps(zg), json.dumps(ft["geometry"])))
                row["coverage"], row["spill"] = round(float(m["coverage"] or 0), 3), round(float(m["spill"] or 0), 3)
            want = [x.strip().lower() for x in (p.get("anchors") or "").split(";") if x.strip()]
            if want:
                row["expected_anchors"] = want
                row["anchors_match"] = all(any(w in a.lower() or a.lower() in w for a in row["anchors"]) for w in want)
            row["likely_cause"] = _advisory_cause(row)
            rows.append(row)
    zone = [r for r in rows if r["expect"] == "zone"]
    neg = [r for r in rows if r["expect"] in ("none", "lifted")]
    parsed = [r for r in zone if not (r["reason"] or "").startswith("no anchor")]
    resolved = [r for r in parsed if "did not resolve" not in (r["reason"] or "")]
    placed = [r for r in zone if r["placed"] and "coverage" in r]
    correct = [r for r in placed if r["coverage"] >= 0.6 and r["spill"] <= 0.6]
    wrong = [r for r in placed if r["coverage"] < 0.1]
    rep.check("A01", len(parsed) / len(zone) if zone else None, "advisory parser (ir.py regex); see failing ids", [r for r in zone if r not in parsed])
    rep.check("A02", len(resolved) / len(parsed) if parsed else None, "gazetteer: see each row's candidates", [r for r in parsed if r not in resolved])
    rep.check("A03", len([r for r in zone if r["placed"]]) / len(zone) if zone else None, "see each row's likely_cause", [r for r in zone if not r["placed"]])
    rep.check("A04", len(correct) / len(placed) if placed else None, "see each row's likely_cause", [r for r in placed if r not in correct])
    rep.check("A05", len(wrong) / len(placed) if placed else None, "see each row's likely_cause", wrong)
    rep.check("A06", sum(1 for r in neg if r["placed"]) / len(neg) if neg else None, "advisory parser: an unrelated or 'cleared' text produced a zone",
              [r for r in neg if r["placed"]])
    rep.detail("advisories", rows)


def _advisory_cause(r: dict) -> str | None:
    if r["expect"] in ("none", "lifted"):
        return "advisory parser: produced a zone for a text with no road action" if r["placed"] else None
    reason = r["reason"] or ""
    if reason.startswith("no anchor"):
        return "advisory parser: no template matched; add the sentence shape to ir.py, or switch on the LLM path"
    if "did not resolve" in reason:
        return ("gazetteer: name absent from OSM or the alias table; add an alias or check the name in OSM" if not r["candidates"]
                else "gazetteer: candidates exist but scored below the threshold or tied; check resolve thresholds or add an alias")
    if r.get("anchors_match") is False:
        return "gazetteer: resolved to a different place than the one you named"
    if r["placed"] and "coverage" in r:
        if r["coverage"] < 0.1:
            return "zone builder: anchors right but the corridor or buffer is drawn elsewhere (zones.py)" if r.get("anchors_match") else "gazetteer or zone builder: zone is not on the marked stretch"
        if r["coverage"] < 0.6:
            return "zone builder: fell back to a buffer round one place instead of the corridor (method from_anchor_fallback)"
        if r["spill"] > 0.6:
            return "zone builder: buffer for this cause is larger than the closure"
    return None


# ------------------------------------------------------------------------------------------------ latency


def latency(c, rep: Report, warm_ms: list[int]):
    print("\n[latency] /v1/route timing and the database's own view")
    ps = read_pairs()
    if not ps:
        ps = random_pairs(c, 10)
    if not ps:
        for cid in ("L01", "L02", "L03"):
            rep.skip(cid, "no pairs to time")
        return
    try:
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        c.execute("SELECT pg_stat_statements_reset()")
        pss = True
    except psycopg.Error:
        pss = False
    ms = []
    with http() as h:
        for _round in range(3):
            for p in ps:
                for v in p["vehicles"]:
                    t0 = time.perf_counter()
                    r = h.get("/v1/route", params={"profile": v, "from": p["from"], "to": p["to"]})
                    if r.status_code == 200:
                        ms.append((time.perf_counter() - t0) * 1000)
    if not ms:
        rep.skip("L01", "no route succeeded")
        rep.skip("L02", "no route succeeded")
    else:
        q = statistics.quantiles(ms, n=20) if len(ms) >= 2 else [ms[0]] * 19
        rep.check("L01", round(statistics.median(ms)), "see pg_stat_statements in details: the database share of the time")
        rep.check("L02", round(q[18]), "routing weights SQL: the edge-cost query over the search window; a wide window on a long trip")
    if pss:
        top = [dict(r) for r in c.execute("""SELECT calls, round(mean_exec_time::numeric, 1) mean_ms, round(total_exec_time::numeric) total_ms,
                                                    left(regexp_replace(query, '\\s+', ' ', 'g'), 140) query
                                             FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 8""").fetchall()]
        dj = one(c, "SELECT sum(total_exec_time) / NULLIF(sum(calls), 0) m FROM pg_stat_statements WHERE query ILIKE '%pgr_bdDijkstra%'")
        rep.check("L03", round(float(dj["m"])) if dj and dj["m"] else None, "see the top statements in details")
        rep.detail("pg_stat_statements_top", top)
    else:
        rep.skip("L03", "pg_stat_statements not loaded (shared_preload_libraries)")
    rep.detail("latency_ms", {"samples": len(ms), "first_pass_ms": warm_ms})


# ------------------------------------------------------------------------------------------------ idempotency

_SCHEMA_FP = """
SELECT md5(string_agg(x, '|' ORDER BY x)) fp FROM (
  SELECT 'c:' || table_schema || '.' || table_name || '.' || column_name || ':' || data_type x FROM information_schema.columns
   WHERE table_schema IN ('net','geo','ingest','flow','hydro','telemetry','client','meta')
  UNION ALL SELECT 'i:' || schemaname || '.' || indexname FROM pg_indexes WHERE schemaname IN ('net','geo','ingest','flow','hydro','telemetry','client')
  UNION ALL SELECT 'f:' || n.nspname || '.' || p.proname || ':' || md5(p.prosrc) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname IN ('net','geo','ingest','client')
  UNION ALL SELECT 's:vp:' || count(*) FROM net.vehicle_profile
  UNION ALL SELECT 's:ab:' || count(*) FROM geo.abbreviation
  UNION ALL SELECT 's:ct:' || count(*) FROM geo.city
  UNION ALL SELECT 's:src:' || count(*) FROM ingest.source) q"""

_NET_FP = """
SELECT count(*) edges, md5(string_agg(osm_way_id || ':' || seg || ':' || src || ':' || dst || ':' || d.static_mask || ':' || round(length_m),
                                      ',' ORDER BY osm_way_id, seg)) fp
FROM net.edge e JOIN net.edge_dim d USING (edge_id)"""

_GAZ_FP = """
SELECT md5(string_agg(feature_class || ':' || n, ',' ORDER BY feature_class)) fp,
       (SELECT count(*) FROM net.edge WHERE road_gaz_id IS NOT NULL) linked
FROM (SELECT feature_class, count(*) n FROM geo.gazetteer GROUP BY 1) q"""

_HISTORY = """SELECT (SELECT count(*) FROM net.gate) gates, (SELECT count(*) FROM hydro.sink) sinks,
                     (SELECT count(*) FROM telemetry.hpvse_alert) hpvse,
                     (SELECT count(*) FROM net.gate g CROSS JOIN LATERAL unnest(g.cut_set) c(edge_id)
                      WHERE NOT EXISTS (SELECT 1 FROM net.edge e WHERE e.edge_id = c.edge_id)) stale_cut_refs"""


def _sql_file(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("\\"))


def idempotency(c, rep: Report, reimport: bool):
    print("\n[idempotency] migrations and the import, run a second time")
    before = one(c, _SCHEMA_FP)["fp"]
    errors = []
    has_ts = bool(one(c, "SELECT 1 x FROM pg_extension WHERE extname = 'timescaledb'"))
    with psycopg.connect(DB) as tx:
        for f in sorted((ROOT / "db" / "migrations").glob("*.sql")):
            sql = f.read_text()
            if not has_ts:                       # sandbox databases without TimescaleDB
                sql = sql.replace("CREATE EXTENSION IF NOT EXISTS timescaledb;", "")
            try:
                tx.execute(sql)
                tx.commit()
            except psycopg.Error as e:
                tx.rollback()
                errors.append({"file": f.name, "error": str(e).splitlines()[0][:200]})
    rep.check("I01", len(errors), "migration SQL is not idempotent: see the file and statement", errors)
    rep.check("I02", int(one(c, _SCHEMA_FP)["fp"] != before), "a migration re-creates or re-seeds on a second run")
    if not reimport:
        for cid in ("I03", "I04", "I05"):
            rep.skip(cid, "--skip-reimport")
        return
    if not one(c, "SELECT count(*) n FROM osm.highway")["n"]:
        for cid in ("I03", "I04", "I05"):
            rep.skip(cid, "osm.highway is empty")
        return
    n1, g1, h1 = one(c, _NET_FP), one(c, _GAZ_FP), one(c, _HISTORY)
    t0 = time.time()
    with psycopg.connect(DB, autocommit=True) as tx:
        for f in ("build_network.sql", "build_gazetteer.sql"):
            tx.execute(_sql_file(ROOT / "import" / f))
        tx.execute("SELECT geo.reapply_all_zones()")
    n2, g2, h2 = one(c, _NET_FP), one(c, _GAZ_FP), one(c, _HISTORY)
    rep.check("I03", int(n1 != n2), "import: build_network.sql depends on row order or on state left by the previous run", {"before": n1, "after": n2})
    rep.check("I04", int(g1 != g2), "import: build_gazetteer.sql", {"before": g1, "after": g2})
    lost = sum(max(0, h1[k] - h2[k]) for k in ("gates", "sinks", "hpvse")) + h2["stale_cut_refs"]
    rep.check("I05", lost, "import: build_network.sql dropped rows that reference nodes, or left gate cut sets pointing at old edge ids",
              {"before": h1, "after": h2})
    rep.detail("reimport_seconds", round(time.time() - t0))


def restart_before(c, rep: Report):
    t = one(c, "SELECT now() t")["t"]
    c.execute("UPDATE ingest.source SET last_polled = NULL WHERE enabled")      # every source due now
    rep.d["restart"] = {"marked_at": t.isoformat(), "dupes_before": _dupes(c)}
    print(f"marked at {t.isoformat()}; all enabled sources set due. Kill the worker now.")


def _dupes(c) -> dict:
    return one(c, """SELECT
        (SELECT count(*) FROM (SELECT dedup_hash FROM ingest.raw_message WHERE ts_ingested > now() - interval '1 day' GROUP BY 1 HAVING count(*) > 1) a) messages,
        (SELECT count(*) FROM (SELECT ir_id FROM geo.zone_of_avoidance WHERE ir_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1) b) zones,
        (SELECT count(*) FROM (SELECT sub_id, zone_id, kind FROM client.outbox WHERE kind <> 'SMS_FALLBACK' GROUP BY 1, 2, 3 HAVING count(*) > 1) d) alerts""")


def restart_after(c, rep: Report):
    print("\n[idempotency] worker killed mid-poll and started again")
    started = one(c, "SELECT now() t")["t"]
    c.execute("UPDATE ingest.source SET last_polled = NULL WHERE enabled")     # due again: the restarted worker must pick them up
    deadline = time.time() + 120
    pending = None
    while time.time() < deadline:
        pending = [dict(r) for r in c.execute("SELECT name, kind, last_polled FROM ingest.source WHERE enabled AND (last_polled IS NULL OR last_polled < %s)",
                                              (started,)).fetchall()]
        if not pending:
            break
        time.sleep(5)
    rep.check("I06", len(pending or []), "worker did not come back: docker compose -p detourai logs worker", pending)
    before = (rep.d.get("restart") or {}).get("dupes_before") or {"messages": 0, "zones": 0, "alerts": 0}
    after = _dupes(c)
    rep.check("I07", sum(max(0, after[k] - before.get(k, 0)) for k in after), "pipeline: a half-finished poll was replayed without the dedup check",
              {"before": before, "after": after})


# ------------------------------------------------------------------------------------------------ external: Osmose, blind review


def osmose(c, rep: Report):
    print("\n[external] Osmose quality-assurance issues for the extract (OpenStreetMap France, independent of DetourAI)")
    x0, y0, x1, y1 = rep.d["meta"].get("extent") or [None] * 4
    if x0 is None:
        r = one(c, "SELECT ST_XMin(e) x0, ST_YMin(e) y0, ST_XMax(e) x1, ST_YMax(e) y1 FROM (SELECT ST_Extent(geom)::geometry e FROM net.node) q")
        x0, y0, x1, y1 = r["x0"], r["y0"], r["x1"], r["y1"]
    step, items, total, errors = 0.05, {}, 0, []
    y = y0
    with httpx.Client(timeout=60, headers={"User-Agent": "DetourAI-verify"}) as h:
        while y < y1:
            x = x0
            while x < x1:
                bbox = f"{x:.4f},{y:.4f},{min(x + step, x1):.4f},{min(y + step, y1):.4f}"
                try:
                    j = h.get("https://osmose.openstreetmap.fr/api/0.3/issues", params={"bbox": bbox, "level": "1,2", "limit": 500}).json()
                    for i in j.get("issues", []):
                        items[str(i.get("item"))] = items.get(str(i.get("item")), 0) + 1
                        total += 1
                    if len(j.get("issues", [])) >= 500:
                        errors.append(f"tile {bbox} hit the 500-issue cap; count is a lower bound")
                except (httpx.HTTPError, ValueError) as e:
                    errors.append(f"{bbox}: {str(e)[:80]}")
                x += step
            y += step
    rep.d.setdefault("external", {})["osmose"] = {"bbox": [x0, y0, x1, y1], "issues_level_1_2": total, "by_item": dict(sorted(items.items(), key=lambda kv: -kv[1])[:25]),
                                                    "notes": errors, "fetched_at": now(),
                                                    "how_to_read": "item numbers are listed at https://osmose.openstreetmap.fr/api/0.3/items ; 1xxx are mostly geometry and topology"}
    print(f"  {total} level-1/2 issues in the extract; top items {list(items.items())[:5]}")


def review_pack(c, rep: Report, n: int):
    print("\n[external] blind review pack")
    ps = read_pairs() or random_pairs(c, n)
    if not OSRM:
        print("  OSRM_URL not set: the pack needs OSRM as the second route")
        return
    rnd = random.Random(int(time.time()))
    items, key = [], {}
    with http() as h:
        for p in ps[:n]:
            v = p["vehicles"][0]
            r = h.get("/v1/route", params={"profile": v, "from": p["from"], "to": p["to"]})
            o = osrm_route(p["from"], p["to"])
            if r.status_code != 200 or not o or "error" in o:
                continue
            mine, base = r.json()["geometry"], o["geometry"]
            flip = rnd.random() < 0.5
            items.append({"pair": p["id"], "vehicle": "two-wheeler" if v == "2W" else "car", "label": f"{p.get('from_label', '')} to {p.get('to_label', '')}".strip(),
                          "A": base if flip else mine, "B": mine if flip else base})
            key[p["id"]] = "B" if flip else "A"
    out = VERIFY / "review" / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.html").write_text((VERIFY / "review_template.html").read_text().replace("/*ITEMS*/[]", json.dumps(items)), encoding="utf-8")
    (out / "KEY-do-not-share.json").write_text(json.dumps({"detourai_is": key, "made_at": now()}, indent=2))
    rep.d.setdefault("external", {})["review_pack"] = {"folder": str(out.relative_to(ROOT)), "pairs": len(items)}
    print(f"  {len(items)} pairs written to {out.relative_to(ROOT)}; send only review.html")


def score_review(rep: Report, answers: Path, keyfile: Path):
    print("\n[external] blind review scored")
    key = json.loads(keyfile.read_text())["detourai_is"]
    with answers.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    good, flags, scored = 0, [], 0
    for r in rows:
        k = key.get(r["pair"])
        if not k:
            continue
        scored += 1
        pick = (r.get("preferred") or "").strip().upper()
        good += pick in (k, "SAME")
        if (r.get(f"impassable_{k}") or "").strip().lower() in ("yes", "y", "1", "true"):
            flags.append({"pair": r["pair"], "note": r.get("notes", "")})
    rep.check("X01", good / scored if scored else None, "read the reviewer's notes per pair; compare with R03 and T02 for the same pairs")
    rep.check("X02", len(flags), "the reviewer names a road DetourAI used that this vehicle cannot use: check V03, V04, V07 for it", flags)


# ------------------------------------------------------------------------------------------------ CIFS feed, lead time

_CIFS_TYPES = {"ROAD_CLOSED", "ACCIDENT", "HAZARD", "POLICE", "CHIT_CHAT", "JAM"}


def feed(c, rep: Report):
    """The Waze-format feed, checked here without using the API's own validator: required fields, limits from the
    specification, unique ids, and that each line really lies on a road carrying the street name it claims."""
    print("\n[feed] closure feed in Waze's CIFS format")
    with http() as h:
        r = h.get("/v1/feeds/cifs.json")
    incs = r.json().get("incidents", []) if r.status_code == 200 else None
    if incs is None:
        rep.check("F01", 1, f"the feed endpoint answered HTTP {r.status_code}")
        return
    pins = one(c, """SELECT count(*) n, count(*) FILTER (WHERE ST_Distance(r.geom::geography, z.geom::geography) > 40) off
                     FROM client.report r JOIN geo.zone_of_avoidance z USING (zone_id) WHERE r.created_at > now() - interval '7 days'""")
    if pins["n"]:
        rep.check("C02", pins["off"], "reports.create: the zone was not drawn at the reported pin (snap or substring logic)")
    else:
        rep.skip("C02", "no crowd reports in the last 7 days")
    if not incs:
        for cid in ("F01", "F02", "C01"):
            rep.skip(cid, "the feed is empty: no active zone is sure enough to publish right now")
        return
    bad, seen = [], set()
    for i in incs:
        p = []
        for k in ("id", "type", "polyline", "street"):
            if not str(i.get(k) or "").strip():
                p.append(f"missing {k}")
        if i.get("id") in seen:
            p.append("duplicate id")
        seen.add(i.get("id"))
        if i.get("type") not in _CIFS_TYPES:
            p.append("unknown type")
        if len(i.get("description") or "") > 40:
            p.append("description over 40 characters")
        if i.get("direction") not in ("BOTH_DIRECTIONS", "ONE_DIRECTION"):
            p.append("no direction")
        if i.get("type") == "ROAD_CLOSED" and not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", i.get("starttime") or ""):
            p.append("closure without a valid starttime")
        try:
            nums = [float(x) for x in (i.get("polyline") or "").split()]
            pts = [(nums[k + 1], nums[k]) for k in range(0, len(nums) - 1, 2)]           # (lng, lat)
        except ValueError:
            pts = []
        if not pts or len(nums) % 2:
            p.append("polyline is not lat lon pairs")
        else:
            wkt = (f"POINT({pts[0][0]} {pts[0][1]})" if len(pts) == 1 else "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in pts) + ")")
            m = one(c, """
                WITH g AS (SELECT ST_SetSRID(ST_GeomFromText(%s), 4326) g),
                rd AS (SELECT ST_Union(ST_Buffer(e.geom::geography, 20)::geometry) b FROM net.edge e, g WHERE e.name = %s AND e.geom && ST_Expand(g.g, 0.001))
                SELECT ST_Length(g.g::geography) len,
                       CASE WHEN ST_Dimension(g.g) = 0 THEN (rd.b IS NOT NULL AND ST_Intersects(g.g, rd.b))::int::float
                            ELSE COALESCE(ST_Length(ST_Intersection(g.g, rd.b)::geography) / NULLIF(ST_Length(g.g::geography), 0), 0) END on_road
                FROM g, rd""", (wkt, i.get("street")))
            if i.get("type") == "ROAD_CLOSED" and (len(pts) < 2 or not 30 <= m["len"] <= 20000):
                p.append(f"closure line is {m['len']:.0f} m (needs 30 m to 20 km)")
            if (m["on_road"] or 0) < 0.8:
                p.append(f"only {100 * (m['on_road'] or 0):.0f}% of the line lies on a road named {i.get('street')!r}")
        if p:
            bad.append({"id": i.get("id"), "street": i.get("street"), "problems": p})
    rep.check("F01", len(bad), "feed builder (api/app/cifs.py): see each incident's problems; an off-road line means the zone's spine does not follow that street", bad)
    rep.detail("feed", {"incidents": len(incs), "closures": sum(1 for i in incs if i.get("type") == "ROAD_CLOSED"), "streets": sorted({i.get("street") for i in incs})[:20]})
    zone_ids = sorted({int(m.group(1)) for i in incs if (m := re.match(r"dz(\d+)-", str(i.get("id") or "")))})
    src = {r["zone_id"]: r for r in c.execute("SELECT zone_id, origin, corroborations, confirm_yes_w FROM geo.zone_of_avoidance WHERE zone_id = ANY(%s)", (zone_ids,)).fetchall()}
    rep.check("F02", sum(1 for z in src.values() if z["origin"] == "FEED"),
              "feed builder: a zone built from a licensed traffic feed (TomTom) is being passed on; cifs._ZONES must exclude origin FEED",
              [z for z, r in src.items() if r["origin"] == "FEED"])
    rep.check("C01", sum(1 for z in src.values() if z["origin"] == "CROWD" and z["corroborations"] < 2 and z["confirm_yes_w"] < 0.5),
              "reports: a crowd report reached the feed with nobody confirming it; check start_confidence against CIFS_MIN_CONFIDENCE",
              [z for z, r in src.items() if r["origin"] == "CROWD" and r["corroborations"] < 2 and r["confirm_yes_w"] < 0.5])


def score_lead(rep: Report, sheet: Path):
    """Your hand-kept sheet: when DetourAI had each zone, and when Waze and Google Maps first showed it."""
    print("\n[external] lead time against Waze and Google Maps (your sheet)")

    def parse(x):
        x = (x or "").strip()
        return datetime.fromisoformat(x) if x else None
    with sheet.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("zone_id") or "").strip() and not r["zone_id"].startswith("#")]
    first, leads, shown = 0, [], {"waze": 0, "gmaps": 0}
    for r in rows:
        d, w, g = parse(r.get("detour_at")), parse(r.get("waze_at")), parse(r.get("gmaps_at"))
        if d is None:
            continue
        shown["waze"] += w is not None
        shown["gmaps"] += g is not None
        others = [x for x in (w, g) if x is not None]
        if not others or d <= min(others):
            first += 1
        if others:
            leads.append((min(others) - d).total_seconds() / 60)
    n = len(rows)
    rep.check("X03", first / n if n else None, "read the rows where an app was earlier: was the closure crowd-visible before the police posted?")
    rep.d.setdefault("external", {})["lead_time"] = {"zones": n, "shown_by_waze": shown["waze"], "shown_by_google_maps": shown["gmaps"],
                                                      "median_lead_min": round(statistics.median(leads), 1) if leads else None}


# ------------------------------------------------------------------------------------------------ summary


def finalize(rep: Report) -> int:
    res = rep.d["results"]
    by = {}
    for cid, r in sorted(res.items()):
        by.setdefault(r["group"], []).append((cid, r))
    print(f"\nDetourAI verification  {rep.d['started_at']}  report: {rep.path.name}")
    for g, items in by.items():
        counts = {s: sum(1 for _, r in items if r["status"] == s) for s in ("pass", "fail", "known_issue", "skip")}
        print(f"  {g:<12} pass {counts['pass']:>2}  fail {counts['fail']:>2}  known {counts['known_issue']:>2}  skip {counts['skip']:>2}")
    fails = [(cid, r) for cid, r in res.items() if r["status"] == "fail"]
    for cid, r in sorted(res.items()):
        if r["status"] in ("fail", "known_issue"):
            tag = "FAIL " if r["status"] == "fail" else "KNOWN"
            print(f"  {tag} {cid} {r['metric']} = {r['value']} (needs {r['op']} {r['threshold']}): {r.get('known_issue') or r.get('likely_cause', '')}")
    ext = rep.d.get("external") or {}
    if "osmose" in ext:
        print(f"  Osmose: {ext['osmose']['issues_level_1_2']} level 1-2 issues in the extract")
    rep.d["finished_at"] = now()
    rep.d["summary"] = {"fail": len(fails), "known_issue": sum(1 for r in res.values() if r["status"] == "known_issue"),
                        "pass": sum(1 for r in res.values() if r["status"] == "pass"), "skip": sum(1 for r in res.values() if r["status"] == "skip")}
    rep.save()
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "restart-before", "restart-after", "feed", "osmose", "review-pack", "score-review", "score-lead", "finalize"])
    ap.add_argument("--sheet")
    ap.add_argument("--report")
    ap.add_argument("--skip-reimport", action="store_true")
    ap.add_argument("--ci-random-pairs", type=int, default=0)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--answers")
    ap.add_argument("--key")
    a = ap.parse_args()
    path = Path(a.report) if a.report else VERIFY / "reports" / f"verify-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    rep = Report.load(path) if path.exists() and a.cmd != "run" else Report(path)
    with connect() as c:
        if a.cmd == "run":
            meta(c, rep)
            graph(c, rep)
            vehicle(c, rep)
            first = pairs(c, rep, a.ci_random_pairs)
            gates(c, rep)
            gpx_tracks(c, rep)
            advisories(c, rep)
            latency(c, rep, first or [])
            idempotency(c, rep, not a.skip_reimport)
            rep.save()
            (VERIFY / "work").mkdir(exist_ok=True)
            (VERIFY / "work" / "current_report.txt").write_text(str(path))
            print(f"\nreport: {path}")
            return 0
        if a.cmd == "restart-before":
            restart_before(c, rep)
        elif a.cmd == "restart-after":
            restart_after(c, rep)
        elif a.cmd == "feed":
            feed(c, rep)
        elif a.cmd == "score-lead":
            score_lead(rep, Path(a.sheet))
        elif a.cmd == "osmose":
            osmose(c, rep)
        elif a.cmd == "review-pack":
            review_pack(c, rep, a.n)
        elif a.cmd == "score-review":
            score_review(rep, Path(a.answers), Path(a.key))
        elif a.cmd == "finalize":
            return finalize(rep)
        rep.save()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                      # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"verify.py failed: {e}", file=sys.stderr)
        sys.exit(2)
