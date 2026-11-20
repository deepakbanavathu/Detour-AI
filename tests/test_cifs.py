"""The Waze CIFS feed: type mapping, direction-ordered polylines, validation against the published rules."""
from datetime import datetime, timezone

from app import cifs

LINE = [[78.4300, 17.4100], [78.4320, 17.4100], [78.4340, 17.4100]]        # GeoJSON order: lng, lat; heading east


def inc(**kw):
    base = {"id": "dz1-r6-1", "type": "ROAD_CLOSED", "subtype": "ROAD_CLOSED_EVENT", "street": "Road No 1", "polyline": cifs.polyline(LINE),
            "direction": "BOTH_DIRECTIONS", "starttime": "2026-11-20T15:39:23+05:30", "endtime": "2026-11-20T17:39:23+05:30", "description": "VIP movement, road closed"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_only_a_full_closure_with_a_line_becomes_road_closed():
    assert cifs.cifs_type("CLOSURE", "VIP", 1.0, True) == ("ROAD_CLOSED", "ROAD_CLOSED_EVENT")
    assert cifs.cifs_type("DIVERSION", "ROADWORK", 1.0, True) == ("ROAD_CLOSED", "ROAD_CLOSED_CONSTRUCTION")
    assert cifs.cifs_type("CLOSURE", "UNKNOWN", 1.0, True) == ("ROAD_CLOSED", None)
    assert cifs.cifs_type("CLOSURE", "VIP", 1.0, False) == ("HAZARD", "HAZARD_ON_ROAD")                # a point cannot close a road
    assert cifs.cifs_type("WATERLOGGING", "WATERLOGGING", 1.0, True) == ("HAZARD", "HAZARD_WEATHER_FLOOD")   # water is shown, not closed
    assert cifs.cifs_type("CLOSURE", "ROADWORK", 0.6, True) == ("HAZARD", "HAZARD_ON_ROAD_CONSTRUCTION")     # partial restriction
    assert cifs.cifs_type("ACCIDENT", "ACCIDENT", 0.5, True) == ("ACCIDENT", None)
    assert cifs.cifs_type("CLOSURE", "TREE_FALL", 0.5, True) == ("HAZARD", "HAZARD_ON_ROAD_OBJECT")


def test_every_mapped_subtype_is_one_the_spec_allows():
    for et in ("CLOSURE", "DIVERSION", "VIP_MOVEMENT", "WATERLOGGING", "ACCIDENT"):
        for cause in ("WATERLOGGING", "VIP", "ROADWORK", "ACCIDENT", "PROCESSION", "TREE_FALL", "GATE", "UNKNOWN"):
            for pen in (0.5, 1.0):
                for line in (True, False):
                    t, sub = cifs.cifs_type(et, cause, pen, line)
                    assert t in cifs.TYPES and (sub is None or sub in cifs.SUBTYPES[t]), (et, cause, pen, line)


def test_polyline_is_lat_lon_with_six_decimals():
    assert cifs.polyline(LINE) == "17.410000 78.430000 17.410000 78.432000 17.410000 78.434000"


def test_orient_follows_the_affected_direction():
    assert cifs.orient(LINE, 90) == LINE and cifs.orient(LINE, 80) == LINE          # eastbound closure: as drawn
    assert cifs.orient(LINE, 270) == LINE[::-1]                                      # westbound: reversed
    assert cifs.orient(LINE, None) == LINE and cifs.orient(LINE[:1], 270) == LINE[:1]


def test_times_are_iso_with_the_india_offset():
    assert cifs.ts(datetime(2026, 11, 20, 10, 9, 23, 500000, tzinfo=timezone.utc)) == "2026-11-20T15:39:23+05:30"


def test_descriptions_fit_wazes_forty_characters():
    assert all(len(d) <= 40 for d in cifs.DESCRIPTION.values())


def test_a_good_closure_and_a_good_point_validate():
    assert cifs.validate(inc()) == []
    assert cifs.validate(inc(type="HAZARD", subtype="HAZARD_WEATHER_FLOOD", polyline="17.410000 78.433800")) == []


def test_validation_catches_what_waze_would_reject():
    def probs(**kw):
        return " | ".join(cifs.validate(inc(**kw)))
    assert "missing street" in probs(street="")
    assert "not a CIFS type" in probs(type="CLOSED")
    assert "does not belong" in probs(subtype="HAZARD_WEATHER_FLOOD")
    assert "a closure needs a line" in probs(polyline="17.41 78.43")
    assert "outside 30 m to 20 km" in probs(polyline="17.410000 78.430000 17.410000 78.430100")          # about 11 m
    assert "not lat lon order" in probs(polyline="78.43 17.41 78.44 17.41".replace("78.43 17.41", "178.43 97.41"))
    assert "lat lon pairs" in probs(polyline="17.41 78.43 17.41")
    assert "needs a starttime" in probs(starttime=None)
    assert "ISO 8601" in probs(endtime="2026-11-20 17:39")
    assert "over 40 characters" in probs(description="x" * 41)
    assert "direction must be" in probs(direction="NORTH")
    assert "single point needs a direction" in " | ".join(cifs.validate(inc(type="HAZARD", subtype=None, polyline="17.41 78.43", direction=None)))


def test_xml_escapes_and_keeps_the_id_as_an_attribute():
    x = cifs.to_xml([inc(street="A & B <Road>", id='z"1')])
    assert "<street>A &amp; B &lt;Road&gt;</street>" in x and "<incident id='z\"1'>" in x and x.startswith("<?xml")
    assert "<polyline>17.410000 78.430000" in x and x.rstrip().endswith("</incidents>")


def test_length_helper():
    assert 420 < cifs.length_m(LINE) < 430
