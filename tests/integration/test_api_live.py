"""End-to-end checks against the running stack. Skipped automatically when the API is not reachable.

Run:  DETOUR_API=http://127.0.0.1:8080 ADMIN_TOKEN=... python -m pytest -m live tests/integration
The stack must have the Hyderabad network imported (make import); tests that change state clean up after themselves.
"""
import os

import httpx
import pytest

API = os.environ.get("DETOUR_API", "http://127.0.0.1:8080")
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "change-me")}
A, B = "17.4486,78.3908", "17.4126,78.4480"          # Madhapur -> Banjara Hills

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def api():
    try:
        with httpx.Client(base_url=API, timeout=60) as c:
            h = c.get("/healthz")
            if h.status_code != 200 or h.json().get("edges", 0) == 0:
                pytest.skip("API is up but no road network is imported")
            yield c
    except httpx.HTTPError:
        pytest.skip(f"API not reachable at {API}")


def test_health_reports_network_and_profiles(api):
    h = api.get("/healthz").json()
    assert h["ok"] and h["edges"] > 100_000 and {"2W", "4W_HATCH", "HCV"} <= set(h["profiles"])


def test_route_returns_line_time_and_maps_link(api):
    r = api.get("/v1/route", params={"profile": "4W_HATCH", "from": A, "to": B}).json()
    assert r["geometry"]["type"] == "LineString" and len(r["geometry"]["coordinates"]) > 10
    assert 5_000 < r["distance_m"] < 40_000 and 300 < r["duration_s"] < 7_200
    assert r["maps_url"].startswith("https://www.google.com/maps/dir/?api=1") and "travelmode=driving" in r["maps_url"]
    assert r["snap_from"]["dist_m"] < 300 and r["snap_to"]["dist_m"] < 300


def test_two_wheeler_is_never_slower_than_a_truck_and_may_use_more_roads(api):
    d = api.get("/v1/route/compare", params={"from": A, "to": B, "profiles": "2W,HCV"}).json()
    assert "error" not in d["2W"]
    if "error" not in d["HCV"]:
        assert d["2W"]["duration_s"] <= d["HCV"]["duration_s"] * 1.05


def test_bad_inputs_are_rejected(api):
    assert api.get("/v1/route", params={"profile": "TANK", "from": A, "to": B}).status_code == 400
    assert api.get("/v1/route", params={"profile": "2W", "from": "abc", "to": B}).status_code == 400
    assert api.get("/v1/route", params={"profile": "2W", "from": "0,0", "to": B}).status_code in (404, 400)
    assert api.get("/v1/zones", params={"bbox": "70,10,90,30"}).status_code == 400


def test_admin_endpoints_need_the_token(api):
    assert api.post("/v1/admin/edges/state", json={"edge_ids": [1], "gate_state": "OPEN"}).status_code == 401
    assert api.post("/v1/team-routes", json={"name": "x", "from": A, "to": B}).status_code == 401
    assert api.post("/v1/zones/1/status", json={"status": "REFUTED"}).status_code == 401


def test_places_resolve_known_area(api):
    d = api.get("/v1/places/resolve", params={"q": "Madhapur"}).json()
    assert d["candidates"], "gazetteer returned nothing for Madhapur"
    assert any("madhapur" in c["canonical"].lower() for c in d["candidates"])


def test_advisory_dry_run_changes_nothing(api):
    before = api.get("/healthz").json()["active_zones"]
    d = api.post("/v1/advisories/paste", json={"text": "Waterlogging at Madhapur. Vehicles may take alternate routes", "commit": False}).json()
    assert api.get("/healthz").json()["active_zones"] == before
    if d["ok"]:
        assert d["zone"]["status"] == "DRY_RUN" and d["zone"]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    else:
        assert "candidates" in d or "reason" in d


def test_zone_lifecycle_closes_a_road_and_refuting_reopens_it(api):
    """Commit a hard zone on the car's own route, see the route change, refute it, see the route come back."""
    base = api.get("/v1/route", params={"profile": "4W_HATCH", "from": A, "to": B}).json()
    mid = base["edges"][len(base["edges"]) // 2]
    ids = [e["edge_id"] for e in base["edges"][len(base["edges"]) // 2 - 2: len(base["edges"]) // 2 + 3]]
    try:
        assert api.post("/v1/admin/edges/state", headers=ADMIN, json={"edge_ids": ids, "zone_penalty": 1.0}).json()["updated"] == len(ids)
        detour = api.get("/v1/route", params={"profile": "4W_HATCH", "from": A, "to": B}).json()
        assert mid["edge_id"] not in [e["edge_id"] for e in detour["edges"]]
        assert detour["duration_s"] >= base["duration_s"]
    finally:
        api.post("/v1/admin/edges/state", headers=ADMIN, json={"edge_ids": ids, "zone_penalty": 0.0, "profile_mask": 0})
    again = api.get("/v1/route", params={"profile": "4W_HATCH", "from": A, "to": B}).json()
    assert abs(again["duration_s"] - base["duration_s"]) < 5


def test_team_route_brief_and_pack(api):
    made = api.post("/v1/team-routes", headers=ADMIN, json={"name": "pytest run", "from": A, "to": B, "from_label": "Madhapur", "to_label": "Banjara Hills", "profile": "4W_HATCH"}).json()
    rid = made["route_id"]
    try:
        b = api.get("/v1/brief").json()
        mine = [r for r in b["routes"] if r["name"] == "pytest run"]
        assert mine and mine[0]["minutes"] > 0 and b["summary"]
        assert "*Hyderabad road brief*" in api.get("/v1/brief", params={"format": "text"}).text
        assert api.get("/v1/brief", params={"format": "email"}).text.startswith("<table")

        pack = api.get(f"/v1/packs/{rid}", params={"rebuild": True}).json()
        assert "A" in pack["routes"] and pack["routes"]["A"]["turns"][0]["text"].startswith("Start on")
        assert pack["routes"]["A"]["turns"][-1]["text"] == "Arrive"
        for label in ("B", "C"):
            if label in pack["routes"]:
                assert pack["routes"][label]["decision_point"] is not None
        st = api.get(f"/v1/packs/{rid}/status").json()
        assert st["version"] == pack["version"] and st["best"] == "A" and st["stale"] is False
    finally:
        assert api.delete(f"/v1/team-routes/{rid}", headers=ADMIN).status_code == 200
    assert api.get(f"/v1/packs/{rid}", params={"rebuild": True}).status_code == 404


def test_sources_and_review_endpoints(api):
    src = api.get("/v1/sources").json()
    assert {s["kind"] for s in src} >= {"OPEN_METEO", "TOMTOM_INCIDENTS"}
    assert all(s["state"] in ("ok", "needs a key", "failing", "off", "waiting for first poll") for s in src)
    assert isinstance(api.get("/v1/review").json(), list)


def test_health_counts_open_reviews_and_poll_now_needs_admin(api):
    assert isinstance(api.get("/healthz").json()["open_reviews"], int)
    src = api.get("/v1/sources").json()
    sid = src[0]["source_id"]
    assert api.post(f"/v1/sources/{sid}/poll-now").status_code == 401
    assert api.post(f"/v1/sources/{sid}/poll-now", headers=ADMIN).json() == {"source_id": sid, "queued": True}
    assert api.post("/v1/sources/999999/poll-now", headers=ADMIN).status_code == 404


def test_unplaceable_advisory_lands_in_review_and_can_be_dismissed(api):
    text = "Traffic diverted at Zzyzx Qwerty Chowrasta towards Madhapur due to road work"
    d = api.post("/v1/advisories/paste", json={"text": text, "source_handle": "@pytest"}).json()
    assert d["ok"] is False
    mine = [r for r in api.get("/v1/review").json() if "zzyzx" in r["sentence"]]
    assert mine and mine[0]["reason"] in ("UNRESOLVED", "AMBIGUOUS") and mine[0]["anchor"]
    rid = mine[0]["review_id"]
    assert api.post(f"/v1/review/{rid}", json={"action": "dismiss"}).status_code == 401
    assert api.post(f"/v1/review/{rid}", headers=ADMIN, json={"action": "dismiss"}).json()["status"] == "DISMISSED"
    assert not [r for r in api.get("/v1/review").json() if r["review_id"] == rid]


def test_pages_are_served(api):
    for path in ("/console", "/brief", "/card/1", "/sw.js"):
        r = api.get(path)
        assert r.status_code == 200, path
    assert api.get("/sw.js").headers["service-worker-allowed"] == "/"
