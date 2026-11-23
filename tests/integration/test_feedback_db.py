"""Rider corrections against a real database: reported, grouped, applied, kept through a rebuild of the masks, and
avoided at once by the reporter's next route. Runs with DETOUR_DB set; restores everything it changes."""
import uuid

import pytest
from conftest import forget, long_road, onboard

pytestmark = pytest.mark.db


def test_corrections_from_report_to_route(db):
    c = db
    from app import feedback, packs
    road, pts = long_road(c)
    users = [f"pytest-fb-{uuid.uuid4().hex[:8]}-{k}" for k in "ab"]
    spot = pts[len(pts) // 2]
    edge = c.execute("""SELECT e.edge_id, e.osm_way_id, e.seg, d.width_m, d.width_src, d.static_mask, d.access_mask FROM net.edge e JOIN net.edge_dim d USING (edge_id)
                        ORDER BY e.geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) LIMIT 1""", (spot["lng"], spot["lat"])).fetchone()
    key = (edge["osm_way_id"], edge["seg"])
    try:
        a = onboard(c, users[0], pts[0], pts[-1], "veh_4w")
        b = onboard(c, users[1], pts[0], pts[-1], "veh_4w")
        assert edge["static_mask"] & 4, "the test needs a road a car may use"

        # two car drivers say it is too narrow; a third answer was only their own choice
        for sub in (a, b):
            r = feedback.record(c, sub, "TOO_NARROW", "4W_HATCH", spot["lat"], spot["lng"])
            assert r == {"ok": True, "edge_id": edge["edge_id"], "road": road}
        feedback.record(c, a, "CHOICE", "4W_HATCH", spot["lat"], spot["lng"])
        mine = [x for x in feedback.open_corrections(c) if (x["osm_way_id"], x["seg"]) == key]
        assert len(mine) == 1 and mine[0]["kind"] == "TOO_NARROW" and mine[0]["people"] == 2 and mine[0]["reports"] == 2
        assert mine[0]["title"] == "Too narrow for a car" and "2.0 m" in mine[0]["apply"]
        unchanged = c.execute("SELECT static_mask FROM net.edge_dim WHERE edge_id = %s", (edge["edge_id"],)).fetchone()["static_mask"]
        assert unchanged == edge["static_mask"], "nothing changes for everyone until an operator applies it"

        # the reporter's own next route already steers round it
        token = c.execute("SELECT token FROM client.corridor WHERE sub_id = %s ORDER BY label LIMIT 1", (a,)).fetchone()["token"]
        fresh = packs.trip_reroute(token, (pts[0]["lat"], pts[0]["lng"]), avoid=[edge["edge_id"]])
        assert fresh["rerouted"] is True and fresh["routes"]["A"]["turns"][0]["text"].startswith("Start on")

        # an operator applies it: cars are shut out, scooters are not, and the change is remembered by OSM way and segment
        out = feedback.close(c, *key, "TOO_NARROW", "apply")
        assert out["closed"] == 2 and out["edges_changed"] >= 1
        d = c.execute("SELECT width_m, width_src, static_mask FROM net.edge_dim WHERE edge_id = %s", (edge["edge_id"],)).fetchone()
        assert abs(d["width_m"] - 2.0) < 1e-6 and d["width_src"] == "FEEDBACK" and d["static_mask"] & 4 == 0 and d["static_mask"] & 1 == 1
        assert not [x for x in feedback.open_corrections(c) if (x["osm_way_id"], x["seg"]) == key]

        # a re-import rebuilds widths from OSM; the correction is put back
        c.execute("UPDATE net.edge_dim SET width_m = %s, width_src = %s, static_mask = %s WHERE edge_id = %s", (edge["width_m"], edge["width_src"], edge["static_mask"], edge["edge_id"]))
        assert c.execute("SELECT net.corrections_unapplied() AS n").fetchone()["n"] == 1
        c.execute("SELECT net.apply_corrections()")
        assert c.execute("SELECT net.corrections_unapplied() AS n").fetchone()["n"] == 0
        assert c.execute("SELECT static_mask FROM net.edge_dim WHERE edge_id = %s", (edge["edge_id"],)).fetchone()["static_mask"] & 4 == 0

        # a gate report marks the gate closed when applied; dismissing leaves the map alone
        feedback.record(c, b, "GATE_CLOSED", "4W_HATCH", spot["lat"], spot["lng"])
        assert feedback.close(c, *key, "GATE_CLOSED", "dismiss") == {"closed": 1, "edges_changed": 0}
        feedback.record(c, b, "GATE_CLOSED", "4W_HATCH", spot["lat"], spot["lng"])
        assert feedback.close(c, *key, "GATE_CLOSED", "apply")["edges_changed"] == 1
        assert c.execute("SELECT gate_state FROM net.edge_dynamic WHERE edge_id = %s", (edge["edge_id"],)).fetchone()["gate_state"] == "CLOSED_REPORTED"

        # far from any road, and the daily limit
        assert feedback.record(c, a, "NO_ENTRY", "4W_HATCH", 0.0, 0.0) == {"ok": False, "reason": "no_road"}
        for _ in range(feedback.PER_DAY):
            feedback.record(c, a, "CHOICE", "4W_HATCH", spot["lat"], spot["lng"])
        assert feedback.record(c, a, "CHOICE", "4W_HATCH", spot["lat"], spot["lng"]) == {"ok": False, "reason": "limit"}
    finally:
        c.rollback()
        c.execute("DELETE FROM net.correction WHERE osm_way_id = %s AND seg = %s", key)
        c.execute("DELETE FROM client.route_feedback WHERE osm_way_id = %s AND seg = %s", key)
        c.execute("UPDATE net.edge_dim SET width_m = %s, width_src = %s, static_mask = %s, access_mask = %s WHERE edge_id = %s",
                  (edge["width_m"], edge["width_src"], edge["static_mask"], edge["access_mask"], edge["edge_id"]))
        c.execute("UPDATE net.edge_dynamic SET gate_state = 'OPEN', p_open_now = 1 WHERE edge_id = %s", (edge["edge_id"],))
        c.commit()
        forget(c, users, [])
