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
    """Signed with the admin token, as a script would be."""
    try:
        with httpx.Client(base_url=API, timeout=60, headers=ADMIN) as c:
            h = c.get("/healthz")
            if h.status_code != 200 or h.json().get("edges", 0) == 0:
                pytest.skip("API is up but no road network is imported")
            yield c
    except httpx.HTTPError:
        pytest.skip(f"API not reachable at {API}")


@pytest.fixture(scope="module")
def anon(api):
    """No token and no session: what a stranger on the network gets."""
    with httpx.Client(base_url=API, timeout=60) as c:
        yield c


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


def test_admin_endpoints_need_the_token(api, anon):
    assert anon.post("/v1/admin/edges/state", json={"edge_ids": [1], "gate_state": "OPEN"}).status_code == 401
    assert anon.post("/v1/team-routes", json={"name": "xx", "from": A, "to": B}).status_code == 401
    assert anon.post("/v1/zones/1/status", json={"status": "REFUTED"}).status_code == 401


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


def test_health_counts_open_reviews_and_poll_now_needs_admin(api, anon):
    assert isinstance(api.get("/healthz").json()["open_reviews"], int)
    src = api.get("/v1/sources").json()
    sid = src[0]["source_id"]
    assert anon.post(f"/v1/sources/{sid}/poll-now").status_code == 401
    assert api.post(f"/v1/sources/{sid}/poll-now", headers=ADMIN).json() == {"source_id": sid, "queued": True}
    assert api.post("/v1/sources/999999/poll-now", headers=ADMIN).status_code == 404


def test_unplaceable_advisory_lands_in_review_and_can_be_dismissed(api, anon):
    text = "Traffic diverted at Zzyzx Qwerty Chowrasta towards Madhapur due to road work"
    d = api.post("/v1/advisories/paste", headers=ADMIN, json={"text": text, "source_handle": "@pytest", "commit": True}).json()
    assert d["ok"] is False
    mine = [r for r in api.get("/v1/review").json() if "zzyzx" in r["sentence"]]
    assert mine and mine[0]["reason"] in ("UNRESOLVED", "AMBIGUOUS") and mine[0]["anchor"]
    rid = mine[0]["review_id"]
    assert anon.post(f"/v1/review/{rid}", json={"action": "dismiss"}).status_code == 401
    assert api.post(f"/v1/review/{rid}", headers=ADMIN, json={"action": "dismiss"}).json()["status"] == "DISMISSED"
    assert not [r for r in api.get("/v1/review").json() if r["review_id"] == rid]


def test_webhooks_reject_unsigned_calls_and_unknown_links(api, anon):
    assert anon.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code == 403
    assert anon.post("/webhooks/whatsapp", content=b'{"entry":[]}', headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 403
    assert anon.post("/webhooks/whatsapp", content=b'{"entry":[]}').status_code == 403
    assert anon.post("/webhooks/telegram", json={"message": {"chat": {"id": 1}, "text": "hi"}}).status_code == 403
    assert anon.get("/r/not-a-real-token", follow_redirects=False).status_code == 404


def test_channel_status_shape(api):
    d = api.get("/v1/channels").json()
    assert set(d) == {"whatsapp", "telegram", "sms", "links"}
    assert isinstance(d["whatsapp"]["configured"], bool) and isinstance(d["whatsapp"]["active"], int)
    assert isinstance(d["links"]["alerts_sent"], int)


def test_cifs_feed_is_well_formed_json_and_xml(api):
    j = api.get("/v1/feeds/cifs.json").json()
    assert list(j) == ["incidents"] and isinstance(j["incidents"], list)
    for i in j["incidents"]:
        assert {"id", "type", "polyline", "street", "direction"} <= set(i) and len(i.get("description", "")) <= 40
    x = api.get("/v1/feeds/cifs.xml")
    assert x.headers["content-type"].startswith("application/xml") and x.text.lstrip().startswith("<?xml") and "<incidents" in x.text
    pv = api.get("/v1/feeds/cifs/preview").json()
    assert pv["incidents"] == len(j["incidents"]) and isinstance(pv["skipped"], list)
    assert api.get("/v1/route", params={"profile": "2W", "from": A, "to": B}).json()["waze_url"].startswith("https://waze.com/ul?ll=")


def test_saving_an_advisory_is_an_admin_action(api, anon):
    text = "Waterlogging at Madhapur. Vehicles may take alternate routes"
    before = api.get("/healthz").json()["active_zones"]
    assert anon.post("/v1/advisories/paste", json={"text": text, "commit": True}).status_code == 401
    assert anon.post("/v1/advisories/paste", json={"text": text, "commit": True, "source_cred": 1.0}, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert anon.post("/v1/advisories/paste", json={"text": text, "force_status": "ACTIVE"}).status_code == 401
    assert api.post("/v1/advisories/paste", json={"text": text}).status_code == 200            # the default is a dry run
    assert api.get("/healthz").json()["active_zones"] == before


def test_public_address_reaches_only_the_public_surface(api):
    host = os.environ.get("DETOUR_PUBLIC_HOST")                       # the host part of PUBLIC_BASE_URL, when the stack has one
    if not host:
        pytest.skip("DETOUR_PUBLIC_HOST not set")
    h = {"Host": host}
    for path in ("/console", "/brief", "/healthz", "/v1/sources", "/v1/review", "/v1/feeds/cifs/preview"):
        assert api.get(path, headers=h).status_code == 404, path
    assert api.post("/v1/advisories/paste", headers=h, json={"text": "Waterlogging at Madhapur today"}).status_code == 404
    assert api.get("/v1/feeds/cifs.json", headers=h).status_code == 200 and api.get("/sw.js", headers=h).status_code == 200
    assert api.get("/console").status_code == 200                     # the local address is unaffected


def test_sign_in_guards_pages_and_data(api, anon):
    if not anon.get("/v1/session").json()["login_required"]:
        pytest.skip("REQUIRE_LOGIN is off on this stack")
    r = anon.get("/console", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/console"
    assert anon.get("/brief", follow_redirects=False).status_code == 303
    for path in ("/v1/brief", "/v1/zones?bbox=78.3,17.3,78.5,17.5", "/v1/sources", "/v1/review", "/v1/channels", "/static/console.html", "/docs"):
        assert anon.get(path).status_code == 401, path
    assert anon.get("/v1/route", params={"profile": "2W", "from": A, "to": B}).status_code == 401
    for path in ("/login", "/healthz", "/sw.js", "/v1/feeds/cifs.json"):
        assert anon.get(path).status_code == 200, path

    with httpx.Client(base_url=API, timeout=60) as c:                       # a browser: wrong password, right password, sign out
        assert c.post("/v1/login", json={"password": "definitely-wrong"}).status_code == 401
        ok = c.post("/v1/login", json={"password": ADMIN["X-Admin-Token"]})
        assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
        assert c.get("/v1/session").json()["signed_in"] is True
        assert c.get("/console").status_code == 200 and c.get("/v1/sources").status_code == 200
        sid = c.get("/v1/sources").json()[0]["source_id"]
        assert c.post(f"/v1/sources/{sid}/poll-now").status_code == 200       # the session carries admin rights
        c.post("/v1/logout")
        assert c.get("/v1/sources").status_code == 401


def test_repeat_route_is_answered_from_memory_until_something_changes(api):
    q = {"profile": "4W_HATCH", "from": A, "to": B}
    first = api.get("/v1/route", params=q)
    again = api.get("/v1/route", params=q)
    assert first.json()["distance_m"] == again.json()["distance_m"]
    assert again.elapsed <= first.elapsed * 1.5 + __import__("datetime").timedelta(milliseconds=50)
    assert api.get("/v1/route", params=q, headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip"


def test_search_suggests_from_the_first_letters(api):
    for typed in ("ma", "madh", "Madhap"):
        names = [s["name"].lower() for s in api.get("/v1/places/suggest", params={"q": typed}).json()["suggestions"]]
        assert any(n.startswith("madhapur") for n in names), (typed, names)
    assert api.get("/v1/places/suggest", params={"q": "zzqqxx"}).json()["suggestions"] == []
    assert api.get("/v1/places/suggest", params={"q": "m"}).status_code == 422
    s = api.get("/v1/places/suggest", params={"q": "madh"}).json()["suggestions"][0]
    assert {"name", "kind", "lat", "lng", "source"} <= set(s) and 17.2 < s["lat"] < 17.7


def test_map_feeds_for_the_console_and_the_waze_page(api):
    z = api.get("/v1/zones/active").json()
    assert z["city"] and 17 < z["center"]["lat"] < 18 and isinstance(z["zones"], list)
    assert isinstance(api.get("/v1/reports").json()["reports"], list)
    assert api.get("/waze").status_code == 200


def test_trip_links_are_unguessable_and_public_only_by_token(anon):
    for bad in ("abc", "0" * 31, "G" * 32, "0" * 33):
        assert anon.get(f"/go/{bad}").status_code == 404
    unknown = "0123456789abcdef0123456789abcdef"                       # right shape, no such trip
    assert anon.get(f"/go/{unknown}").status_code == 200               # the page itself holds nothing personal
    assert anon.get(f"/v1/trips/{unknown}").status_code == 404
    assert anon.get(f"/v1/trips/{unknown}/status").status_code == 404
    assert anon.post(f"/v1/trips/{unknown}/report", json={"kind": "WATER", "lat": 17.4, "lng": 78.4}).status_code == 404
    assert anon.get("/app-icon.svg").headers["content-type"].startswith("image/svg+xml")


def test_corrections_list_and_apply_need_the_right_access(api, anon):
    assert isinstance(api.get("/v1/corrections").json()["corrections"], list)
    body = {"osm_way_id": 1, "seg": 0, "kind": "TOO_NARROW", "action": "apply"}
    assert anon.post("/v1/corrections", json=body).status_code == 401
    assert api.post("/v1/corrections", json=body).json() == {"closed": 0, "edges_changed": 0}       # nothing open for that stretch
    assert api.post("/v1/corrections", json={**body, "kind": "PAINT_IT_RED"}).status_code == 422
    unknown = "0123456789abcdef0123456789abcdef"
    assert anon.post(f"/v1/trips/{unknown}/feedback", json={"kind": "TOO_NARROW", "lat": 17.4, "lng": 78.4}).status_code == 404
    assert anon.get(f"/v1/trips/{unknown}/reroute", params={"lat": 17.4, "lng": 78.4}).status_code == 404


def test_trip_events_and_usage_endpoints(api, anon):
    unknown = "0123456789abcdef0123456789abcdef"
    assert anon.post(f"/v1/trips/{unknown}/events", json={"events": []}).status_code == 404
    assert anon.post(f"/v1/trips/{unknown}/events", json={"events": [{}] * 21}).status_code == 422      # one batch at most
    assert anon.get("/v1/usage").status_code == 401
    u = api.get("/v1/usage", params={"days": 7}).json()
    assert {"started", "arrived", "switched", "left_route", "riders", "skips_open", "alert_opens"} <= set(u)
    bad = {"osm_way_id": 1, "seg": 0, "kind": "SKIPPED", "action": "apply"}
    assert api.post("/v1/corrections", json=bad).json() == {"closed": 0, "edges_changed": 0} or api.post("/v1/corrections", json=bad).status_code == 400


def test_pages_are_served(api):
    for path in ("/console", "/brief", "/card/1", "/sw.js"):
        r = api.get(path)
        assert r.status_code == 200, path
    assert api.get("/sw.js").headers["service-worker-allowed"] == "/"
