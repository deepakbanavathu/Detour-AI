"""Route packs: alternates, decision points and turn lists (pure functions)."""
from app import packs


def edge(i, length, name, bearing, lat=17.40, lng=78.40):
    return {"edge_id": i, "length_m": length, "name": name, "bearing": bearing, "start_lat": lat + i * 0.001, "start_lng": lng + i * 0.001}


def test_shared_fraction_is_by_length_of_the_candidate():
    cand = [edge(1, 100, "a", 0), edge(2, 300, "b", 0), edge(3, 600, "c", 0)]
    assert packs.shared_fraction(cand, {1, 2}) == 0.4
    assert packs.shared_fraction(cand, set()) == 0.0 and packs.shared_fraction([], {1}) == 1.0


def test_decision_index_is_the_common_prefix():
    assert packs.decision_index([1, 2, 3, 4], [1, 2, 9, 4]) == 2
    assert packs.decision_index([1, 2], [7, 8]) == 0 and packs.decision_index([1, 2], [1, 2, 3]) == 2


def test_turn_list_reads_like_directions():
    es = [edge(1, 400, "Hitec City Main Road", 90), edge(2, 300, "Hitec City Main Road", 95),     # same road, no turn
          edge(3, 500, "Road No 36", 185),                                                        # right turn onto a new road
          edge(4, 200, "Road No 36", 100),                                                        # sharp left staying on it
          edge(5, 250, None, 110),                                                                # unnamed continuation
          edge(6, 100, "Road No 45", 300)]                                                        # U-turn-ish onto new road
    t = packs.turn_list(es)
    assert [x["text"] for x in t] == ["Start on Hitec City Main Road", "Turn right onto Road No 36", "Turn left to stay on Road No 36",
                                       "Continue onto the unnamed road", "Make a U-turn onto Road No 45", "Arrive"]
    assert [x["at_m"] for x in t] == [0, 700, 1200, 1400, 1650, 1750]


def test_turn_list_edge_cases():
    assert packs.turn_list([]) == []
    one = packs.turn_list([edge(1, 120, None, 10)])
    assert [x["text"] for x in one] == ["Start on the road ahead", "Arrive"] and one[-1]["at_m"] == 120


def test_bear_and_deflection_signs():
    assert packs._verb(packs._deflection(350, 30)) == "Bear right" and packs._verb(packs._deflection(30, 350)) == "Bear left"
    assert packs._verb(packs._deflection(10, 20)) is None


def test_version_changes_when_any_route_changes():
    v1 = packs.version_of({"A": [1, 2, 3], "B": [1, 9, 3]})
    assert v1 == packs.version_of({"B": [1, 9, 3], "A": [1, 2, 3]}) and v1 != packs.version_of({"A": [1, 2, 3], "B": [1, 8, 3]})


def test_best_label_picks_the_stored_route_the_current_one_follows():
    stored = {"A": [[1, 100.0], [2, 300.0], [3, 600.0]], "B": [[1, 100.0], [8, 400.0], [9, 500.0]]}
    cur = [edge(1, 100, "x", 0), edge(8, 400, "y", 0), edge(9, 500, "z", 0)]
    assert packs.best_label(cur, stored) == ("B", 1.0)
    label, overlap = packs.best_label([edge(50, 900, "q", 0), edge(1, 100, "x", 0)], stored)
    assert overlap == 0.1 and overlap < packs.STALE_OVERLAP
