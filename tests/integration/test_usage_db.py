"""Trip events against a real database: stored with the skipped stretch worked out, a correction opened when several
riders skip the same stretch on different days, applied as what the operator decided, and counted for the product
owner. Runs with DETOUR_DB set; removes what it made."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from conftest import forget, long_road, onboard

pytestmark = pytest.mark.db


def test_departures_become_a_correction(db):
    c = db
    from app import feedback, packs, usage
    road, pts = long_road(c)
    users = [f"pytest-use-{uuid.uuid4().hex[:6]}-{k}" for k in "abcd"]
    way = seg = None
    try:
        trips = []
        for u in users:
            sub = onboard(c, u, pts[0], pts[-1])
            token = c.execute("SELECT token FROM client.corridor WHERE sub_id = %s ORDER BY label LIMIT 1", (sub,)).fetchone()["token"]
            packs.trip_get(token)                                        # builds and stores the pack and its edges
            trips.append(packs._trip(c, token))
        edges = trips[0]["pack_edges"]["A"]
        assert len(edges) >= 3, "the test needs a route of at least three stretches"
        # the end of the route's second stretch: a rider leaving here skipped the third
        second, third = int(edges[1][0]), int(edges[2][0])
        p = c.execute("""SELECT ST_Y(pt) lat, ST_X(pt) lng FROM (SELECT CASE WHEN e.dst IN (n.src, n.dst) THEN ST_LineInterpolatePoint(e.geom, 0.95)
                                                                         ELSE ST_LineInterpolatePoint(e.geom, 0.05) END AS pt
                         FROM net.edge e, net.edge n WHERE e.edge_id = %s AND n.edge_id = %s) q""", (second, third)).fetchone()
        now = datetime.now(timezone.utc)

        def send(i, days_ago):
            at = (now - timedelta(days=days_ago)).isoformat()
            return usage.record(c, trips[i], [{"kind": "START", "route": "A", "at": at},
                                              {"kind": "OFF_ROUTE", "route": "A", "at": at, "lat": p["lat"], "lng": p["lng"]}], "2W")

        assert send(0, 1) == {"stored": 2, "ignored": 0}
        row = c.execute("SELECT edge_id, osm_way_id, seg, ST_AsText(geom) g FROM client.trip_event WHERE corr_id = %s AND kind = 'OFF_ROUTE'",
                        (trips[0]["corr_id"],)).fetchone()
        assert row["edge_id"] == third and row["g"].startswith("POINT")
        way, seg = row["osm_way_id"], row["seg"]

        send(1, 1)
        assert usage.detect_skips(c) == 0, "two riders are not enough"
        send(2, 0)
        assert usage.detect_skips(c) == 1, "three riders on two days open a correction"
        assert usage.detect_skips(c) == 0, "and only once"
        mine = [x for x in feedback.open_corrections(c) if (x["osm_way_id"], x["seg"]) == (way, seg)]
        assert len(mine) == 1 and mine[0]["kind"] == "SKIPPED" and mine[0]["people"] == 3 and mine[0]["title"] == "Riders leave the route here"

        with pytest.raises(ValueError):
            feedback.close(c, way, seg, "SKIPPED", "apply")              # must say what it is
        c.rollback()
        out = feedback.close(c, way, seg, "SKIPPED", "apply", decide_as="NO_ENTRY")
        assert out["closed"] == 1 and out["edges_changed"] >= 1
        assert c.execute("SELECT kind, deny_mask FROM net.correction WHERE osm_way_id = %s AND seg = %s", (way, seg)).fetchone() == {"kind": "NO_ENTRY", "deny_mask": 1}

        send(3, 0)                                                     # a fourth rider right after the decision
        assert usage.detect_skips(c) == 0, "not re-opened while the operator's decision is recent"

        s = usage.summary(c)
        assert s["started"] >= 4 and s["left_route"] >= 4 and s["riders"] >= 4 and set(s["alert_opens"]) == {"card", "maps", "waze", "alerts"}
    finally:
        c.rollback()
        if way is not None:
            c.execute("DELETE FROM net.correction WHERE osm_way_id = %s AND seg = %s", (way, seg))
            c.execute("DELETE FROM client.route_feedback WHERE osm_way_id = %s AND seg = %s", (way, seg))
            c.execute("UPDATE net.edge_dim d SET access_mask = net.access_mask(t.tags) FROM net.edge e JOIN osm.highway t ON t.way_id = e.osm_way_id "
                      "WHERE d.edge_id = e.edge_id AND e.osm_way_id = %s AND e.seg = %s", (way, seg))
            c.execute("UPDATE net.edge_dim d SET static_mask = net.static_mask(e.oneway, d.width_m, d.clearance_m, d.max_gvw_t, d.layout_private) & d.access_mask "
                      "FROM net.edge e WHERE d.edge_id = e.edge_id AND e.osm_way_id = %s AND e.seg = %s", (way, seg))
            c.commit()
        forget(c, users, [])
