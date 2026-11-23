"""Riders correcting the map: what a group of matching reports claims, and what applying it will do."""
from app import feedback


def test_too_narrow_sets_a_width_just_under_what_that_vehicle_needs():
    assert feedback.narrow_width("4W_HATCH") == 2.0          # a car needs 1.7 + 0.4 = 2.1 m, so 2.0 m closes it to cars
    assert feedback.narrow_width("2W") == 1.1                # a scooter needs 1.2 m
    assert feedback.narrow_width("4W_HATCH") < 1.7 + 0.4 and feedback.narrow_width("2W") < 0.8 + 0.4
    assert feedback.narrow_width("4W_HATCH") >= 0.8 + 0.4, "a road too narrow for a car stays open to scooters"


def test_descriptions_say_who_where_and_what_apply_does():
    d = feedback.describe("TOO_NARROW", ["4W_HATCH"], "Road No 12", 3)
    assert d == {"title": "Too narrow for a car", "detail": "3 riders on Road No 12",
                 "apply": "Set the width to 2.0 m, which closes it to a car and anything wider"}
    assert feedback.describe("TOO_NARROW", ["2W", "4W_HATCH"], None, 1)["detail"] == "1 rider on an unnamed road"
    assert feedback.describe("NO_ENTRY", ["2W"], "Lane 4", 2)["apply"] == "Close this stretch to a two-wheeler"
    assert feedback.describe("GATE_CLOSED", ["2W"], "Colony Road", 1)["title"] == "Gate closed"


def test_no_entry_for_a_car_covers_both_car_sizes_and_never_scooters():
    assert feedback.DENY_BITS["4W_HATCH"] == 12 and feedback.DENY_BITS["2W"] == 1
    assert feedback.DENY_BITS["4W_HATCH"] & 1 == 0
    assert set(feedback.KINDS) == {"BLOCKED", "GATE_CLOSED", "TOO_NARROW", "NO_ENTRY", "CHOICE"}
