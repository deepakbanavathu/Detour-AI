"""Trip events and skipped-stretch detection: what is accepted, which stretch a departure skipped, and when
several departures add up to a correction."""
from datetime import datetime, timedelta, timezone

from app import feedback, usage

NOW = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
EDGES = [{"edge_id": 11, "src": 1, "dst": 2}, {"edge_id": 12, "src": 3, "dst": 2}, {"edge_id": 13, "src": 3, "dst": 4}]   # 12 is traversed backwards


def ev(kind, minutes_ago=1, **kw):
    return {"kind": kind, "at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), **kw}


def test_accept_keeps_well_formed_recent_events_only():
    got = usage.accept([ev("START", route="A"), ev("SWITCH", route="B"), ev("ARRIVE"),
                        ev("OFF_ROUTE", route="A", lat=17.41, lng=78.43),
                        ev("OFF_ROUTE", route="A"),                                       # a departure needs its point
                        ev("TELEPORT"), ev("START", minutes_ago=60 * 49),                 # unknown kind; older than two days
                        ev("START", minutes_ago=-30),                                     # from the future
                        {"kind": "START", "at": "2026-11-23T08:00:00"},                   # no time zone
                        {"kind": "START", "at": "yesterday"},
                        ev("OFF_ROUTE", lat=95, lng=78.4), ev("START", route="Z")], NOW)
    assert [(e["kind"], e["route"]) for e in got] == [("START", "A"), ("SWITCH", "B"), ("ARRIVE", None), ("OFF_ROUTE", "A"), ("START", None)]
    assert got[3]["point"] == (17.41, 78.43) and got[0]["point"] is None
    assert len(usage.accept([ev("START")] * 50, NOW)) == usage.MAX_BATCH


def test_the_skipped_stretch_follows_the_direction_of_travel():
    # on stretch 11 (forward), near its end: the rider took it and skipped 12
    assert usage.skipped_edge(EDGES, 0, 0.9) == 12
    # on stretch 11 near its start: the rider never took 11
    assert usage.skipped_edge(EDGES, 0, 0.1) == 11
    # stretch 12 is driven from node 2 to node 3, against its drawing: 0.2 from its own start is 0.8 along the trip
    assert usage.skipped_edge(EDGES, 1, 0.2) == 13 and usage.skipped_edge(EDGES, 1, 0.8) == 12
    # past the last stretch there is nothing left to skip
    assert usage.skipped_edge(EDGES, 2, 0.9) is None and usage.skipped_edge(EDGES, 7, 0.5) is None


def test_a_correction_needs_several_riders_on_different_days():
    d = lambda way, sub, day: {"osm_way_id": way, "seg": 0, "sub_id": sub, "day": day}     # noqa: E731
    deps = [d(100, 1, "2026-11-20"), d(100, 2, "2026-11-21"), d(100, 3, "2026-11-21"),     # three riders, two days: yes
            d(200, 1, "2026-11-20"), d(200, 1, "2026-11-21"), d(200, 1, "2026-11-22"),     # one rider three times: no
            d(300, 1, "2026-11-22"), d(300, 2, "2026-11-22"), d(300, 3, "2026-11-22"),     # three riders, one day (one event): no
            d(400, 1, "2026-11-20"), d(400, 2, "2026-11-21")]                              # two riders: no
    assert usage.skip_candidates(deps) == [(100, 0)]
    assert usage.skip_candidates(deps, riders=2, days=2) == [(100, 0), (400, 0)]


def test_a_skipped_stretch_is_applied_as_what_the_operator_decided():
    d = feedback.describe("SKIPPED", ["2W"], "Lane 6", 4)
    assert d["title"] == "Riders leave the route here" and "different days" in d["detail"] and "too narrow, no entry or a closed gate" in d["apply"]
    assert feedback.DECIDE_AS == ("TOO_NARROW", "NO_ENTRY", "GATE_CLOSED")
