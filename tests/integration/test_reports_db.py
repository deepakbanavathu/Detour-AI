"""Crowd reports against a real database: report, answers, trust, the feed, and the TomTom exclusion.

Runs only when DETOUR_DB points at a database with a road network imported:
  DETOUR_DB=postgresql://detour:detour@127.0.0.1:5432/detour python -m pytest -m db tests/integration/test_reports_db.py
It creates three throwaway Telegram subscribers and removes everything it made.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db
URL = os.environ.get("DETOUR_DB")


@pytest.fixture(scope="module")
def db():
    if not URL:
        pytest.skip("DETOUR_DB not set")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    import app.db as d
    from app import profiles
    d.pool = ConnectionPool(conninfo=URL, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row}, configure=d._configure)
    profiles.load()
    with d.conn() as c:
        if not c.execute("SELECT count(*) AS n FROM net.edge").fetchone()["n"]:
            pytest.skip("no road network imported")
        yield c
    d.pool.close()


def _state(c, zone_id):
    return c.execute("SELECT status, origin, confidence, confirm_yes_w, confirm_no_w FROM geo.zone_of_avoidance WHERE zone_id = %s", (zone_id,)).fetchone()


def _feed_zone_ids(c):
    from app import cifs
    incidents, _ = cifs.build(c)
    return {int(i["id"][2:].split("-")[0]) for i in incidents}, incidents


def test_report_answers_trust_and_feed(db):
    c = db
    from app.channels import reports, store
    from app import zones

    # the longest named arterial: its two ends are the trip, a point in the middle is the report
    road = c.execute("""SELECT name FROM net.edge WHERE name IS NOT NULL AND highway_rank <= 4 GROUP BY name ORDER BY sum(length_m) DESC LIMIT 1""").fetchone()["name"]
    pts = c.execute("""SELECT ST_Y(ST_LineInterpolatePoint(geom, 0.5)) lat, ST_X(ST_LineInterpolatePoint(geom, 0.5)) lng FROM net.edge
                       WHERE name = %s ORDER BY ST_X(ST_StartPoint(geom)) + ST_Y(ST_StartPoint(geom))""", (road,)).fetchall()
    assert len(pts) >= 3, "the test needs a road of at least three segments"
    home, work, spot = pts[0], pts[-1], pts[len(pts) // 2]
    tag = "pytest-" + uuid.uuid4().hex[:8]
    users = [f"{tag}-{k}" for k in "abc"]
    made_zones = []
    try:
        for u in users:
            for m in ({"kind": "text", "text": "hi"}, {"kind": "button", "id": "consent_yes"}, {"kind": "button", "id": "veh_2w"},
                      {"kind": "location", "lat": home["lat"], "lng": home["lng"]}, {"kind": "location", "lat": work["lat"], "lng": work["lng"]},
                      {"kind": "button", "id": "win_both"}):
                store.handle_inbound(c, "TELEGRAM", u, m)
        sub = {u: store._sub_id(c, "TELEGRAM", store.ident("TELEGRAM", u)) for u in users}
        assert all(sub.values()), "onboarding did not create the subscribers"
        a, b, cc = (sub[u] for u in users)

        # a new reporter's pin becomes an unconfirmed crowd zone on that road, not yet in the feed
        store.handle_inbound(c, "TELEGRAM", users[0], {"kind": "text", "text": "REPORT"})
        store.handle_inbound(c, "TELEGRAM", users[0], {"kind": "button", "id": "rep_water"})
        reply = store.handle_inbound(c, "TELEGRAM", users[0], {"kind": "location", "lat": spot["lat"], "lng": spot["lng"]})
        assert road in reply[0]["text"]
        rep = c.execute("SELECT report_id, zone_id, outcome FROM client.report WHERE sub_id = %s ORDER BY report_id DESC LIMIT 1", (a,)).fetchone()
        zid = rep["zone_id"]
        made_zones.append(zid)
        z = _state(c, zid)
        assert (z["status"], z["origin"]) == ("CANDIDATE", "CROWD") and abs(z["confidence"] - 0.6) < 1e-6
        off = c.execute("SELECT ST_Distance(r.geom::geography, z.geom::geography) d FROM client.report r JOIN geo.zone_of_avoidance z USING (zone_id) WHERE r.report_id = %s",
                        (rep["report_id"],)).fetchone()["d"]
        assert off <= 40
        assert zid not in _feed_zone_ids(c)[0]

        # an answer from someone who was never asked is ignored
        assert reports.vote(c, zid, b, True)["effect"] == "IGNORED" and _state(c, zid)["status"] == "CANDIDATE"

        # the people whose route passes are asked (sending fails without a bot token; the question is still recorded)
        reports.ask(c)
        asked = {r["sub_id"] for r in c.execute("SELECT sub_id FROM client.zone_check WHERE zone_id = %s", (zid,)).fetchall()}
        assert {b, cc} <= asked and a not in asked, "the reporter is not asked about their own report"

        # one unproven yes is not enough; the second activates it and it enters the feed on the right street
        assert reports.vote(c, zid, b, True)["effect"] == "KEEP"
        assert reports.vote(c, zid, cc, True)["effect"] == "ACTIVATE" and _state(c, zid)["status"] == "ACTIVE"
        ids, incidents = _feed_zone_ids(c)
        assert zid in ids
        mine = [i for i in incidents if i["id"].startswith(f"dz{zid}-")]
        assert mine and all(i["street"] == road and i["type"] == "HAZARD" for i in mine)

        # the reporter's trust rises once their report is confirmed
        reports.settle(c)
        assert c.execute("SELECT outcome FROM client.report WHERE report_id = %s", (rep["report_id"],)).fetchone()["outcome"] == "CONFIRMED"
        assert abs(c.execute("SELECT trust FROM client.subscriber WHERE sub_id = %s", (a,)).fetchone()["trust"] - 0.6) < 1e-6

        # changing an answer replaces it; two unproven No do not end it; the proven reporter's No does
        assert reports.vote(c, zid, b, False)["effect"] == "KEEP" and reports.vote(c, zid, cc, False)["effect"] == "KEEP"
        s = _state(c, zid)
        assert abs(s["confirm_yes_w"]) < 1e-6 and abs(s["confirm_no_w"] - 0.6) < 1e-6
        assert reports.vote(c, zid, a, False)["effect"] == "REFUTE" and _state(c, zid)["status"] == "REFUTED"
        assert zid not in _feed_zone_ids(c)[0]

        # a zone built from a licensed traffic feed is routed around but never published
        line = c.execute("SELECT ST_AsGeoJSON(geom)::json g FROM net.edge WHERE name = %s ORDER BY length_m DESC LIMIT 1", (road,)).fetchone()["g"]
        now = datetime.now(timezone.utc)
        tz = zones.zone_from_geometry(c, line, "CLOSURE", "UNKNOWN", 1.0, 0.9, now, now + timedelta(hours=1))
        out = zones.commit_zone(c, tz, None, force_status="ACTIVE")
        made_zones.append(out["zone_id"])
        assert _state(c, out["zone_id"])["origin"] == "FEED" and out["zone_id"] not in _feed_zone_ids(c)[0]
    finally:
        c.rollback()
        for u in users:
            idh = store.ident("TELEGRAM", u)
            c.execute("DELETE FROM client.subscriber WHERE channel = 'TELEGRAM' AND channel_id_hmac = %s", (idh,))
            c.execute("DELETE FROM client.chat_state WHERE channel = 'TELEGRAM' AND channel_id_hmac = %s", (idh,))
        c.execute("DELETE FROM client.report WHERE zone_id = ANY(%s)", (made_zones,))
        c.execute("UPDATE geo.zone_of_avoidance SET status = 'EXPIRED' WHERE zone_id = ANY(%s) AND status <> 'REFUTED'", (made_zones,))
        c.execute("SELECT geo.reapply_all_zones()")
        c.commit()
