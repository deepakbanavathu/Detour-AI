"""Google Maps handoff: waypoint selection and the URL contract."""
import math
from urllib.parse import parse_qs, urlparse

from app.maps import maps_url, waypoints


def edges(spec):
    return [{"edge_id": i, "length_m": length, "highway_rank": rank, "zone_adjacent": z, "mid_lat": 17.4 + i * 0.001, "mid_lng": 78.4 + i * 0.001}
            for i, (rank, length, z) in enumerate(spec)]


def test_pins_the_off_arterial_run_and_never_a_zone_edge():
    pins = waypoints(edges([(3, 2000, False), (6, 700, False), (6, 500, False), (2, 1000, False), (6, 400, True), (3, 3000, False)]))
    assert len(pins) == 1 and math.isclose(pins[0][0], 17.401)


def test_at_most_three_pins_in_route_order():
    pins = waypoints(edges([(6, 500, False), (2, 800, False), (6, 900, False), (2, 800, False), (6, 400, False), (2, 800, False), (6, 1200, False)]))
    assert [round(p[0] - 17.4, 3) for p in pins] == [0.0, 0.002, 0.006]


def test_short_runs_and_all_arterial_routes_get_no_pins():
    assert waypoints(edges([(2, 5000, False), (6, 250, False), (3, 4000, False)])) == []
    assert waypoints(edges([(1, 9000, False)])) == []


def test_url_contract():
    q = parse_qs(urlparse(maps_url("2W", (17.4126, 78.4480), [(17.401, 78.401), (17.402, 78.402)])).query)
    assert q["api"] == ["1"] and q["travelmode"] == ["two-wheeler"] and q["destination"] == ["17.41260,78.44800"]
    assert q["waypoints"] == ["17.40100,78.40100|17.40200,78.40200"] and "origin" not in q and "dir_action" not in q
    car = parse_qs(urlparse(maps_url("4W_SUV", (17.4126, 78.4480), [], (17.4486, 78.3908))).query)
    assert car["travelmode"] == ["driving"] and car["origin"] == ["17.44860,78.39080"] and "waypoints" not in car


def test_url_stays_under_the_length_limit():
    assert len(maps_url("LCV", (17.4, 78.4), [(17.41, 78.41), (17.42, 78.42), (17.43, 78.43)], (17.3, 78.3))) < 2048
