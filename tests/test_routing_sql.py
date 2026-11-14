"""The edge-cost SQL the router sends to pgRouting: exclusions, zones on/off, alternates penalty."""
import pglast

from app import profiles, routing, zones

BBOX = (78.3, 17.4, 78.5, 17.5)
TWO_W = profiles.Profile(0, "2W", 0.8, 1.3, 0.16, True, 70)
CAR = profiles.Profile(2, "4W_HATCH", 1.7, 1.55, 0.17, False, 90)


def test_sql_parses_for_every_variant():
    for sql in (routing._edge_sql(TWO_W, BBOX), routing._edge_sql(CAR, BBOX, use_zones=False),
                routing._edge_sql(CAR, BBOX, penalize=[11, 12, 13], penalty=5.0)):
        assert len(pglast.parse_sql(sql)) == 1


def test_profile_bits_and_clearance_are_applied():
    two, car = routing._edge_sql(TWO_W, BBOX), routing._edge_sql(CAR, BBOX)
    assert "(d.static_mask & 1) = 0" in two and "(d.static_mask & 4) = 0" in car
    assert "y.depth_est_m >= 0.16" in two and "y.depth_est_m >= 0.17" in car
    assert "NOT (1 = 1 AND y.two_wheeler_gap)" in two and "NOT (4 = 1 AND y.two_wheeler_gap)" in car     # only 2W uses a gate gap
    assert "true AND d.filterable" in two and "false AND d.filterable" in car                              # only 2W filters


def test_usual_route_ignores_zones_but_keeps_gates_and_water():
    off = routing._edge_sql(CAR, BBOX, use_zones=False)
    assert "zone_penalty_fwd" not in off and "zone_penalty_rev" not in off
    assert "gate_state" in off and "depth_est_m" in off


def test_alternates_penalty_multiplies_only_passable_edges():
    sql = routing._edge_sql(CAR, BBOX, penalize=[11, 12], penalty=2.5)
    assert "ANY('{11,12}'::bigint[]) THEN 2.5 ELSE 1 END" in sql
    assert "ANY(" not in routing._edge_sql(CAR, BBOX, penalize=[11, 12], penalty=1.0)


def test_bbox_grows_with_factor_and_has_a_floor():
    a = routing._bbox((17.44, 78.39), (17.41, 78.45))
    b = routing._bbox((17.44, 78.39), (17.41, 78.45), 2.0)
    assert b[0] < a[0] and b[3] > a[3]
    tiny = routing._bbox((17.44, 78.39), (17.4401, 78.3901))
    assert round(tiny[2] - tiny[0], 3) >= 0.04


def test_scope_to_mask():
    assert zones.scope_to_mask(["ALL"]) == 127 and zones.scope_to_mask(None) == 127
    assert zones.scope_to_mask(["HCV", "BUS"]) == 96 and zones.scope_to_mask(["4W"]) == 12 and zones.scope_to_mask(["???"]) == 127
