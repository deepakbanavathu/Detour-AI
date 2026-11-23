"""A subscriber's live route against a real database: token, pack, the notice when a zone lands on it, the alert that
opens it, and the ROUTE command. Runs with DETOUR_DB set; creates and removes its own rows."""
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from conftest import forget, long_road, onboard

pytestmark = pytest.mark.db
BASE = "https://d.example.test"


def test_trip_page_data_alert_and_route_command(db, monkeypatch):
    c = db
    from app import packs, zones
    from app.channels import fanout, store, telegram

    road, pts = long_road(c)
    assert len(pts) >= 3
    user = "pytest-trip-" + uuid.uuid4().hex[:8]
    made = []
    try:
        sub = onboard(c, user, pts[0], pts[-1])
        trips = c.execute("SELECT label, token FROM client.corridor WHERE sub_id = %s ORDER BY label", (sub,)).fetchall()
        assert [t["label"] for t in trips] == ["home-work", "work-home"]
        assert all(packs.valid_token(t["token"]) for t in trips) and trips[0]["token"] != trips[1]["token"]
        token = trips[0]["token"]

        # the pack: the person's own trip, addressed only by the token
        pack = packs.trip_get(token)
        assert pack["name"] == "Morning trip" and pack["profile"] == "2W" and pack["token"] == token
        assert pack["routes"]["A"]["turns"][0]["text"].startswith("Start on")
        before = pack["notice"]                                         # None on a quiet day; a real city may already have a zone on this road
        assert pack["waze_url"].startswith("https://waze.com/ul?") and pack["maps_url"].startswith("https://www.google.com/maps/")
        assert packs.trip_status(token)["best"] == "A"
        for bad in ("0" * 32, "nope", token.upper(), token[:-1]):
            with pytest.raises(LookupError):
                packs.trip_get(bad)

        # a closure lands on the usual road: the rebuilt pack says why today differs
        line = c.execute("SELECT ST_AsGeoJSON(geom)::json g FROM net.edge WHERE name = %s ORDER BY length_m DESC LIMIT 1", (road,)).fetchone()["g"]
        now = datetime.now(timezone.utc)
        z = zones.zone_from_geometry(c, line, "CLOSURE", "ROADWORK", 1.0, 0.9, now, now + timedelta(hours=2))
        z.origin = "ADVISORY"
        zid = zones.commit_zone(c, z, None, force_status="ACTIVE")["zone_id"]
        made.append(zid)
        pack2 = packs.trip_get(token, rebuild=True)
        assert pack2["notice"] and pack2["notice"] != before
        assert pack2["notice"].startswith("Road work") or "closures on your usual road" in pack2["notice"]
        assert "Route A goes round" in pack2["notice"] or "No way round" in pack2["notice"]
        assert any(f["properties"]["zone_id"] == zid and f["properties"]["label"] == "Road work" for f in pack2["zones"]["features"])

        # the alert for that zone opens the live route first, with Maps and Waze as choices
        sent = []
        monkeypatch.setattr(fanout, "PUBLIC_BASE_URL", BASE)
        monkeypatch.setattr(telegram, "send", lambda payload, *a, **k: (sent.append(payload) or ("m1", None)))
        monday_8am_ist = datetime(2026, 11, 23, 2, 30, tzinfo=timezone.utc)
        out = fanout.fan_out_zone(c, zid, now=monday_8am_ist)
        assert out["sent"] == 1 and len(sent) == 1
        rows = sent[0]["reply_markup"]["inline_keyboard"]
        assert [r[0]["text"] for r in rows] == ["Open live route", "Open in Google Maps", "Open in Waze"]
        assert all(re.fullmatch(BASE + r"/r/[\w-]{8,}", r[0]["url"]) for r in rows)
        a = c.execute("SELECT token, waze_token, card_token FROM client.alert_sent WHERE sub_id = %s AND zone_id = %s", (sub, zid)).fetchone()
        target = c.execute("SELECT target_url FROM client.short_link WHERE token = %s", (a["card_token"],)).fetchone()["target_url"]
        assert target == f"{BASE}/go/{token}" and len({a["token"], a["waze_token"], a["card_token"]}) == 3

        # ROUTE in chat hands back both links
        monkeypatch.setattr(store, "PUBLIC_BASE_URL", BASE)
        reply = store.handle_inbound(c, "TELEGRAM", user, {"kind": "text", "text": "route"})[0]["text"]
        assert f"Morning: {BASE}/go/{trips[0]['token']}" in reply and f"Evening: {BASE}/go/{trips[1]['token']}" in reply
        assert "Keep these links to yourself" in reply

        # DELETE removes the trip: the link stops working
        store.handle_inbound(c, "TELEGRAM", user, {"kind": "text", "text": "DELETE"})
        with pytest.raises(LookupError):
            packs.trip_get(token)
    finally:
        forget(c, [user], made)
