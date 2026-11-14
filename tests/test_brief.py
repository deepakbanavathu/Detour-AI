"""The road brief: summary sentence, WhatsApp text, email HTML."""
from app import brief

ZONES = [{"zone_id": 12, "cause": "Waterlogging", "where": "Jubilee Hills Check Post to Madhapur, detour via Road No 36", "who": "all vehicles",
          "until": "9:40 am", "clearing": False, "closed": True, "sources": 2},
         {"zone_id": 13, "cause": "VIP movement", "where": "between LB Nagar Junction and Nagole", "who": "cars, light trucks",
          "until": "12:00 pm", "clearing": True, "closed": True, "sources": 1}]
ROUTES = [{"route_id": 1, "name": "Office run", "vehicle": "Car", "minutes": 33, "km": 10.9, "usual_minutes": 21, "delay_minutes": 12,
           "affected": True, "rerouted": True, "via": ["Hitec City Main Road", "Road No 1 Banjara Hills"], "maps_url": "https://www.google.com/maps/dir/?api=1&destination=17.4126%2C78.448"},
          {"route_id": 3, "name": "Night drop", "vehicle": "Two-wheeler", "error": "no path", "affected": True},
          {"route_id": 2, "name": "Warehouse to Kukatpally", "vehicle": "Light truck", "minutes": 41, "km": 17.2, "usual_minutes": 41,
           "delay_minutes": 0, "affected": False, "rerouted": False, "via": ["NH 65"], "maps_url": "u"}]


def make(zones=ZONES, routes=ROUTES):
    return {"city": "Hyderabad", "date_label": "Saturday 14 November", "time_label": "7:30 am", "generated_at": "x",
            "zones": zones, "routes": routes, "summary": brief._summary(zones, routes)}


def test_summary_names_the_worst_route():
    assert brief._summary(ZONES, ROUTES) == "2 zones to avoid. 2 of your 3 saved routes are affected; the worst is Office run, 12 min over its usual time."


def test_summary_all_clear_and_no_routes():
    assert brief._summary([], ROUTES).startswith("No diversions on record")
    assert brief._summary(ZONES[:1], []) == "1 zone to avoid. No team routes saved yet."
    assert brief._summary(ZONES, ROUTES[2:]) == "2 zones to avoid; none of them touch your 1 saved route."


def test_whatsapp_text():
    t = brief.as_text(make())
    lines = t.splitlines()
    assert lines[0] == "*Hyderabad road brief*" and "*Avoid (2)*" in lines and "*Your routes*" in lines
    assert "• Waterlogging: Jubilee Hills Check Post to Madhapur, detour via Road No 36. Until about 9:40 am" in lines
    assert "• VIP movement: between LB Nagar Junction and Nagole. Clearing, until about 12:00 pm (cars, light trucks)" in lines
    assert "• Office run (Car): 33 min, 10.9 km, 12 min over usual" in lines and "  Go via Hitec City Main Road, Road No 1 Banjara Hills" in lines
    assert "• Warehouse to Kukatpally (Light truck): 41 min, 17.2 km, as usual" in lines
    assert t.count("https://www.google.com/maps") == 1, "only affected routes carry a link"
    assert "no way through" in t


def test_email_html_is_table_based_and_escaped():
    b = make(routes=[dict(ROUTES[0], name="A & B <run>")])
    h = brief.as_email_html(b)
    assert h.startswith("<table") and "<script" not in h and "A &amp; B &lt;run&gt;" in h
    assert "Open in Google Maps" in h and "Hyderabad road brief" in h and "style=" in h


def test_place_case_and_where_and_who():
    assert brief._place_case("lb nagar junction") == "LB Nagar Junction" and brief._place_case("road no 36") == "Road No 36"
    ir = {"anchors": [{"role": "FROM", "text": "lingampally cross roads"}, {"role": "TO", "text": "post office junction"}], "detour": {"via": "barkatpura chaman"}}
    assert brief._where(ir, 9) == "Lingampally Cross Roads to Post Office Junction, detour via Barkatpura Chaman"
    assert brief._where({"anchors": [{"role": "AT", "text": "tolichowki underpass"}]}, 9) == "at Tolichowki Underpass"
    assert brief._where(None, 9) == "zone 9"
    assert brief._who(127) == "all vehicles" and brief._who(12) == "cars" and brief._who(48) == "light trucks, trucks"
