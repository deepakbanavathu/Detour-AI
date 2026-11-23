"""Riders correct the map from the road: what was wrong with the route, where they are.

One person's word changes nothing for everyone else. Reports about the same stretch are grouped into a correction that
an operator applies with one click; applied width and access corrections survive re-imports. The reporter's own next
route avoids the stretch at once.
"""
from __future__ import annotations

from .config import CITY_ID

KINDS = ("BLOCKED", "GATE_CLOSED", "TOO_NARROW", "NO_ENTRY", "CHOICE")
PER_DAY = 10
SNAP_M = 40.0
VEHICLE_WORDS = {"2W": "a two-wheeler", "4W_HATCH": "a car"}
PROFILE_WIDTH = {"2W": 0.8, "4W_HATCH": 1.7}
DENY_BITS = {"2W": 1, "4W_HATCH": 12}                 # no entry for a car covers hatchback and SUV


def narrow_width(profile: str) -> float:
    """The widest the road can be if this vehicle did not fit: just under its width plus the 0.4 m the fit rule needs."""
    return round(PROFILE_WIDTH.get(profile, 1.7) + 0.3, 2)


def describe(kind: str, profiles: list[str], road: str | None, people: int) -> dict:
    """What the console shows for a group of matching reports: the claim, and what Apply will do."""
    where = road or "an unnamed road"
    who = f"{people} rider{'s' if people != 1 else ''}"
    widest = max(profiles, key=lambda p: PROFILE_WIDTH.get(p, 0), default="4W_HATCH")
    v = VEHICLE_WORDS.get(widest, "a vehicle")
    if kind == "TOO_NARROW":
        return {"title": f"Too narrow for {v}", "detail": f"{who} on {where}", "apply": f"Set the width to {narrow_width(widest)} m, which closes it to {v} and anything wider"}
    if kind == "NO_ENTRY":
        return {"title": f"No entry for {v}", "detail": f"{who} on {where}", "apply": f"Close this stretch to {v}"}
    if kind == "GATE_CLOSED":
        return {"title": "Gate closed", "detail": f"{who} on {where}", "apply": "Mark the gate closed until someone reports it open"}
    return {"title": "Road blocked", "detail": f"{who} on {where}", "apply": "Handled as a road report; nothing to apply here"}


def record(c, sub_id: int | None, kind: str, profile: str, lat: float, lng: float) -> dict:
    """Store one person's feedback, matched to the road stretch they mean."""
    if sub_id is not None:
        n = c.execute("SELECT count(*) AS n FROM client.route_feedback WHERE sub_id = %s AND created_at > now() - interval '20 hours'", (sub_id,)).fetchone()["n"]
        if n >= PER_DAY:
            return {"ok": False, "reason": "limit"}
    # a gate report means the barrier, so prefer a stretch that touches one; otherwise the nearest stretch
    e = c.execute(
        """
        SELECT e.edge_id, e.osm_way_id, e.seg, e.name
        FROM (SELECT ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326) AS pt) p
        JOIN net.edge e ON e.geom && ST_Expand(p.pt, 0.0006)
        LEFT JOIN net.node a ON a.node_id = e.src LEFT JOIN net.node b ON b.node_id = e.dst
        WHERE ST_DWithin(e.geom::geography, p.pt::geography, %(snap)s)
        ORDER BY (%(kind)s = 'GATE_CLOSED' AND COALESCE(a.barrier, b.barrier) IS NOT NULL) DESC, e.geom <-> p.pt
        LIMIT 1
        """, {"lat": lat, "lng": lng, "snap": SNAP_M, "kind": kind}).fetchone()
    if e is None:
        return {"ok": False, "reason": "no_road"}
    c.execute(
        "INSERT INTO client.route_feedback (sub_id, kind, profile, geom, edge_id, osm_way_id, seg) VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)",
        (sub_id, kind, profile, lng, lat, e["edge_id"], e["osm_way_id"], e["seg"]))
    c.commit()
    return {"ok": True, "edge_id": int(e["edge_id"]), "road": e["name"]}


def open_corrections(c, city_id: int = CITY_ID) -> list[dict]:
    """Open feedback grouped by road stretch and kind, most-reported first. 'My choice' and 'blocked' are not corrections."""
    rows = c.execute(
        """
        SELECT f.osm_way_id, f.seg, f.kind, count(*) AS reports, count(DISTINCT COALESCE(f.sub_id, -f.fb_id)) AS people,
               array_agg(DISTINCT f.profile) AS profiles, max(f.created_at) AS latest,
               e.edge_id, e.name, d.width_m, d.width_src, y.gate_state,
               ST_Y(ST_LineInterpolatePoint(e.geom, 0.5)) AS lat, ST_X(ST_LineInterpolatePoint(e.geom, 0.5)) AS lng
        FROM client.route_feedback f
        JOIN net.edge e ON e.osm_way_id = f.osm_way_id AND e.seg = f.seg
        JOIN net.edge_dim d ON d.edge_id = e.edge_id JOIN net.edge_dynamic y ON y.edge_id = e.edge_id
        WHERE f.status = 'OPEN' AND f.kind IN ('GATE_CLOSED','TOO_NARROW','NO_ENTRY')
        GROUP BY f.osm_way_id, f.seg, f.kind, e.edge_id, e.name, d.width_m, d.width_src, y.gate_state, e.geom
        ORDER BY count(DISTINCT COALESCE(f.sub_id, -f.fb_id)) DESC, max(f.created_at) DESC
        LIMIT 100
        """).fetchall()
    out = []
    for r in rows:
        out.append({"edge_id": r["edge_id"], "osm_way_id": r["osm_way_id"], "seg": r["seg"], "kind": r["kind"], "reports": r["reports"], "people": r["people"],
                    "latest": r["latest"], "road": r["name"], "width_m": r["width_m"], "width_src": r["width_src"], "gate_state": r["gate_state"],
                    "lat": r["lat"], "lng": r["lng"], **describe(r["kind"], r["profiles"], r["name"], r["people"])})
    return out


def close(c, osm_way_id: int, seg: int, kind: str, action: str, by: str = "console") -> dict:
    """Apply or dismiss every open report of one kind on one stretch."""
    rows = c.execute(
        "UPDATE client.route_feedback SET status = %s, closed_at = now(), closed_by = %s WHERE osm_way_id = %s AND seg = %s AND kind = %s AND status = 'OPEN' RETURNING profile",
        ("APPLIED" if action == "apply" else "DISMISSED", by, osm_way_id, seg, kind)).fetchall()
    if not rows:
        return {"closed": 0, "edges_changed": 0}
    changed = 0
    if action == "apply":
        profiles = sorted({r["profile"] for r in rows}, key=lambda p: PROFILE_WIDTH.get(p, 0))
        if kind == "TOO_NARROW":
            # the narrowest vehicle that did not fit sets the width
            c.execute("INSERT INTO net.correction (osm_way_id, seg, kind, width_m, applied_by) VALUES (%s, %s, 'TOO_NARROW', %s, %s) "
                      "ON CONFLICT (osm_way_id, seg, kind) DO UPDATE SET width_m = LEAST(net.correction.width_m, EXCLUDED.width_m), applied_at = now(), applied_by = EXCLUDED.applied_by",
                      (osm_way_id, seg, narrow_width(profiles[0]), by))
            changed = c.execute("SELECT net.apply_corrections() AS n").fetchone()["n"]
        elif kind == "NO_ENTRY":
            deny = 0
            for p in profiles:
                deny |= DENY_BITS.get(p, 0)
            c.execute("INSERT INTO net.correction (osm_way_id, seg, kind, deny_mask, applied_by) VALUES (%s, %s, 'NO_ENTRY', %s, %s) "
                      "ON CONFLICT (osm_way_id, seg, kind) DO UPDATE SET deny_mask = net.correction.deny_mask | EXCLUDED.deny_mask, applied_at = now(), applied_by = EXCLUDED.applied_by",
                      (osm_way_id, seg, deny, by))
            changed = c.execute("SELECT net.apply_corrections() AS n").fetchone()["n"]
        elif kind == "GATE_CLOSED":
            changed = c.execute(
                "UPDATE net.edge_dynamic y SET gate_state = 'CLOSED_REPORTED', p_open_now = 0, updated_at = now() "
                "FROM net.edge e WHERE e.edge_id = y.edge_id AND e.osm_way_id = %s AND e.seg = %s", (osm_way_id, seg)).rowcount
    c.commit()
    return {"closed": len(rows), "edges_changed": changed}
