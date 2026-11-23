"""What happens on the live route page, and what DetourAI learns from it.

Riders who agree send a handful of events per trip: start, switch, leaving the route, arrival. No track is kept. A
departure carries the last point on the route; the server works out which stretch of the route came next, because
that is the stretch the rider chose not to take. When several riders skip the same stretch on different days,
DetourAI opens a correction by itself, the way Grab notices skipped roads, and an operator decides what it means.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import CITY_ID

KINDS = ("START", "SWITCH", "OFF_ROUTE", "ARRIVE")
MAX_BATCH, MAX_PER_DAY = 20, 200
MAX_AGE = timedelta(days=2)                 # an event from a phone that was offline for longer is not believed
SKIP_RIDERS, SKIP_DAYS, SKIP_WINDOW_DAYS = 3, 2, 14      # estimates: tune on real departures


# ---------------------------------------------------------------- pure helpers (unit-tested)

def accept(events: list[dict], now: datetime) -> list[dict]:
    """Keep well-formed events from the last two days and not from the future; at most one batch."""
    out = []
    for e in events[:MAX_BATCH]:
        try:
            at = datetime.fromisoformat(str(e.get("at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None or e.get("kind") not in KINDS or not (now - MAX_AGE <= at <= now + timedelta(minutes=5)):
            continue
        route = e.get("route") if e.get("route") in ("A", "B", "C") else None
        lat, lng = e.get("lat"), e.get("lng")
        point = (float(lat), float(lng)) if e["kind"] == "OFF_ROUTE" and isinstance(lat, (int, float)) and isinstance(lng, (int, float)) \
            and -90 <= lat <= 90 and -180 <= lng <= 180 else None
        if e["kind"] == "OFF_ROUTE" and point is None:
            continue
        out.append({"kind": e["kind"], "route": route, "at": at, "point": point})
    return out


def skipped_edge(edges: list[dict], point_edge: int, fraction: float) -> int | None:
    """Which stretch the rider chose not to take. edges: the route in travel order as [{edge_id, src, dst}];
    point_edge: index of the stretch nearest the last point on the route; fraction: how far along that stretch the
    point lies, measured from its own start node. Past half-way the rider took this stretch and skipped the next."""
    if not 0 <= point_edge < len(edges):
        return None
    e = edges[point_edge]
    nxt = edges[point_edge + 1] if point_edge + 1 < len(edges) else None
    prv = edges[point_edge - 1] if point_edge > 0 else None
    # travel direction: forward when this stretch's end node is where the next one starts (or its start where the last ended)
    forward = (e["dst"] in (nxt["src"], nxt["dst"])) if nxt else (e["src"] in (prv["src"], prv["dst"])) if prv else True
    along = fraction if forward else 1.0 - fraction
    if along < 0.5:
        return int(e["edge_id"])
    return int(nxt["edge_id"]) if nxt else None


def skip_candidates(departures: list[dict], riders: int = SKIP_RIDERS, days: int = SKIP_DAYS) -> list[tuple]:
    """Stretches skipped by at least `riders` different riders on at least `days` different days.
    departures: [{osm_way_id, seg, sub_id, day}]"""
    by: dict[tuple, dict] = {}
    for d in departures:
        k = (d["osm_way_id"], d["seg"])
        g = by.setdefault(k, {"riders": set(), "days": set()})
        g["riders"].add(d["sub_id"])
        g["days"].add(d["day"])
    return sorted(k for k, g in by.items() if len(g["riders"]) >= riders and len(g["days"]) >= days)


# ---------------------------------------------------------------- database

def record(c, trip: dict, events: list[dict], profile: str) -> dict:
    """Store accepted events for one trip. trip: the corridor row with its stored pack edges."""
    n_today = c.execute("SELECT count(*) AS n FROM client.trip_event WHERE corr_id = %s AND received > now() - interval '1 day'", (trip["corr_id"],)).fetchone()["n"]
    kept = accept(events, datetime.now(timezone.utc))[: max(0, MAX_PER_DAY - n_today)]
    stored = 0
    for e in kept:
        edge = way = seg = None
        geom = None
        if e["kind"] == "OFF_ROUTE":
            lat, lng = e["point"]
            geom = f"SRID=4326;POINT({lng} {lat})"
            stored_edges = (trip.get("pack_edges") or {}).get(e["route"] or "A")
            if stored_edges:
                ids = [int(x[0]) for x in stored_edges]
                rows = c.execute(
                    """
                    SELECT x.i - 1 AS idx, e.edge_id, e.src, e.dst,
                           ST_Distance(e.geom::geography, p.pt::geography) AS dist, ST_LineLocatePoint(e.geom, p.pt) AS f
                    FROM unnest(%s::bigint[]) WITH ORDINALITY AS x(id, i) JOIN net.edge e ON e.edge_id = x.id,
                         (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS pt) p
                    ORDER BY x.i
                    """, (ids, lng, lat)).fetchall()
                if rows:
                    near = min(rows, key=lambda r: r["dist"])
                    if near["dist"] <= 80:                    # the point really is on this route
                        edge = skipped_edge([{"edge_id": r["edge_id"], "src": r["src"], "dst": r["dst"]} for r in rows], near["idx"], float(near["f"]))
            if edge is not None:
                w = c.execute("SELECT osm_way_id, seg FROM net.edge WHERE edge_id = %s", (edge,)).fetchone()
                way, seg = (w["osm_way_id"], w["seg"]) if w else (None, None)
        c.execute(
            "INSERT INTO client.trip_event (corr_id, sub_id, kind, route, profile, geom, edge_id, osm_way_id, seg, at) "
            "VALUES (%s, %s, %s, %s, %s, ST_GeomFromEWKT(%s), %s, %s, %s, %s)",
            (trip["corr_id"], trip["sub_id"], e["kind"], e["route"], profile, geom, edge, way, seg, e["at"]))
        stored += 1
    c.commit()
    return {"stored": stored, "ignored": len(events) - stored}


def detect_skips(c, now: datetime | None = None) -> int:
    """Open a 'riders leave the route here' correction for each stretch several riders skipped on different days."""
    now = now or datetime.now(timezone.utc)
    rows = c.execute(
        """
        SELECT osm_way_id, seg, sub_id, (at AT TIME ZONE 'Asia/Kolkata')::date AS day
        FROM client.trip_event
        WHERE kind = 'OFF_ROUTE' AND osm_way_id IS NOT NULL AND at > %s
          -- a rider who said 'my choice' at that stretch was not avoiding it
          AND NOT EXISTS (SELECT 1 FROM client.route_feedback f WHERE f.kind = 'CHOICE' AND f.sub_id = trip_event.sub_id
                          AND f.osm_way_id = trip_event.osm_way_id AND f.seg = trip_event.seg)
        """, (now - timedelta(days=SKIP_WINDOW_DAYS),)).fetchall()
    made = 0
    for way, seg in skip_candidates([dict(r) for r in rows]):
        if c.execute("SELECT 1 FROM client.route_feedback WHERE osm_way_id = %s AND seg = %s AND kind = 'SKIPPED' AND status = 'OPEN'", (way, seg)).fetchone():
            continue
        # an operator decided about this stretch recently (applied as whatever it turned out to be, or dismissed):
        # the departures that led to that decision must not open it again
        if c.execute("SELECT 1 FROM client.route_feedback WHERE osm_way_id = %s AND seg = %s AND status <> 'OPEN' AND closed_at > %s",
                     (way, seg, now - timedelta(days=SKIP_WINDOW_DAYS))).fetchone():
            continue
        e = c.execute("SELECT edge_id, ST_LineInterpolatePoint(geom, 0.5) AS mid FROM net.edge WHERE osm_way_id = %s AND seg = %s", (way, seg)).fetchone()
        if e is None:
            continue
        profile = c.execute("SELECT mode() WITHIN GROUP (ORDER BY profile) AS p FROM client.trip_event WHERE osm_way_id = %s AND seg = %s AND kind = 'OFF_ROUTE'",
                            (way, seg)).fetchone()["p"] or "2W"
        c.execute("INSERT INTO client.route_feedback (sub_id, kind, profile, geom, edge_id, osm_way_id, seg) VALUES (NULL, 'SKIPPED', %s, %s, %s, %s, %s)",
                  (profile, e["mid"], e["edge_id"], way, seg))
        made += 1
    c.commit()
    return made


def summary(c, days: int = 7, city_id: int = CITY_ID) -> dict:
    """For the product owner: are people using the live route, and does it get them there."""
    r = c.execute(
        """
        WITH ev AS (SELECT * FROM client.trip_event WHERE at > now() - make_interval(days => %s)),
        trips AS (SELECT corr_id, (at AT TIME ZONE 'Asia/Kolkata')::date AS day,
                         bool_or(kind = 'ARRIVE') AS arrived, count(*) FILTER (WHERE kind = 'SWITCH') AS switches,
                         count(*) FILTER (WHERE kind = 'OFF_ROUTE') AS departures
                  FROM ev GROUP BY 1, 2 HAVING bool_or(kind = 'START'))
        SELECT (SELECT count(*) FROM trips) AS started,
               (SELECT count(*) FROM trips WHERE arrived) AS arrived,
               (SELECT count(*) FROM trips WHERE switches > 0) AS switched,
               (SELECT count(*) FROM trips WHERE departures > 0) AS left_route,
               (SELECT count(DISTINCT sub_id) FROM ev) AS riders,
               (SELECT count(*) FROM client.route_feedback WHERE kind = 'SKIPPED' AND status = 'OPEN') AS skips_open
        """, (days,)).fetchone()
    opens = c.execute(
        """SELECT count(*) FILTER (WHERE opened_in = 'card') AS card, count(*) FILTER (WHERE opened_in = 'maps') AS maps,
                  count(*) FILTER (WHERE opened_in = 'waze') AS waze, count(*) AS alerts
           FROM client.alert_sent WHERE sent_at > now() - make_interval(days => %s)""", (days,)).fetchone()
    return {"days": days, **dict(r), "alert_opens": dict(opens)}
