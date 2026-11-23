"""Type-ahead place search: literal matching of what was typed, and the fallback geocoder's answer."""
from app import gazetteer, geocode

BBOX = (78.20, 17.20, 78.75, 17.65)


def test_typed_text_is_literal_inside_the_pattern():
    assert gazetteer.like_escape("100%_road") == "100\\%\\_road"
    assert gazetteer.like_escape("a\\b") == "a\\\\b" and gazetteer.like_escape("madhapur") == "madhapur"


def test_suggest_sql_ranks_name_start_then_word_start_then_anywhere():
    s = gazetteer._SUGGEST_SQL
    assert s.index("%(prefix)s THEN 0") < s.index("%(word)s THEN 1") < s.index("%(inside)s THEN 2")
    assert "a.alias LIKE %(inside)s OR a.alias %% %(q)s" in s                 # trigram match still catches misspellings


PHOTON = {"features": [
    {"geometry": {"type": "Point", "coordinates": [78.3915, 17.4483]}, "properties": {"name": "Madhapur", "osm_value": "suburb", "city": "Hyderabad"}},
    {"geometry": {"type": "Point", "coordinates": [78.3920, 17.4490]}, "properties": {"name": "madhapur", "osm_value": "neighbourhood"}},          # same name again
    {"geometry": {"type": "Point", "coordinates": [77.5946, 12.9716]}, "properties": {"name": "Madhapur Layout", "osm_value": "suburb"}},         # Bengaluru: outside
    {"geometry": {"type": "Point", "coordinates": [78.40, 17.45]}, "properties": {"osm_value": "primary"}},                                       # no name
    {"geometry": {"type": "Polygon", "coordinates": []}, "properties": {"name": "Madhapur Lake"}},                                                # not a point
    {"geometry": {"type": "Point", "coordinates": [78.3890, 17.4410]}, "properties": {"name": "Madhapur Police Station", "osm_value": "police", "district": "Madhapur"}}]}


def test_geocoder_answer_keeps_named_points_inside_the_city_once_each():
    out = geocode.parse(PHOTON, BBOX)
    assert [o["name"] for o in out] == ["Madhapur", "Madhapur Police Station"]
    assert out[0] == {"gaz_id": None, "name": "Madhapur", "kind": "area", "lat": 17.4483, "lng": 78.3915, "source": "osm-search", "near": "Hyderabad"}
    assert out[1]["kind"] == "police" and out[1]["near"] == "Madhapur"
    assert geocode.parse({}, BBOX) == [] and geocode.parse({"features": [{}]}, BBOX) == []


def test_geocoder_is_skipped_for_short_text_or_when_switched_off(monkeypatch):
    assert geocode.search("mad", BBOX) == []                                    # under four letters: gazetteer only
    monkeypatch.setattr(geocode, "GEOCODER_URL", "")
    assert geocode.search("madhapur", BBOX) == []
