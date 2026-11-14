"""Advisory text -> IR. Each case is a post shape seen from Indian traffic police or the news that quotes them."""
import pytest

from app import ir

CASES = [
    # id, normalized text, event, cause, {role: text}
    ("diversion_with_via", "traffic diverted from jubilee hills check post towards madhapur via road no 36 due to waterlogging",
     "DIVERSION", "WATERLOGGING", {"FROM": "jubilee hills check post", "TO": "madhapur", "VIA": "road no 36"}),
    ("waterlogging_at", "waterlogging at tolichowki underpass. vehicles may take alternate routes",
     "WATERLOGGING", "WATERLOGGING", {"AT": "tolichowki underpass"}),
    ("vip_between", "traffic restricted between lb nagar junction and nagole due to vip movement from 10 am to 12 pm",
     "VIP_MOVEMENT", "VIP", {"BETWEEN_A": "lb nagar junction", "BETWEEN_B": "nagole"}),
    ("heavy_vehicles", "heavy vehicles diverted at pv narasimha rao expressway towards gachibowli owing to road work",
     "DIVERSION", "ROADWORK", {"FROM": "pv narasimha rao expressway", "TO": "gachibowli"}),
    ("closure_till", "vehicles closed at khairatabad flyover for repair work till 6 pm",
     "CLOSURE", "ROADWORK", {"FROM": "khairatabad flyover"}),
    ("lifted", "traffic restored and normal at begumpet after accident cleared", "LIFTED", "ACCIDENT", {"AT": "begumpet"}),
    ("procession", "traffic will be diverted from charminar towards madina via gulzar houz in view of the procession",
     "DIVERSION", "PROCESSION", {"FROM": "charminar", "TO": "madina", "VIA": "gulzar houz"}),
    ("tree_fall", "tree fall near paradise circle, traffic slow moving", "CLOSURE", "TREE_FALL", {"AT": "paradise circle"}),
]


@pytest.mark.parametrize("name,text,event,cause,roles", CASES, ids=[c[0] for c in CASES])
def test_template_shapes(name, text, event, cause, roles):
    out = ir.regex_ir(text)
    assert out is not None, "no anchor found"
    assert (out["event_type"], out["cause"]) == (event, cause)
    assert {a["role"]: a["text"] for a in out["anchors"]} == roles
    assert ir.validate_ir(out)


def test_full_police_form_marks_the_shut_stretch_not_the_detour():
    out = ir.regex_ir("traffic from kachiguda cross roads towards post office junction will be diverted at "
                      "lingampally cross roads towards tourist junction via barkatpura chaman.")
    assert {a["role"]: a["text"] for a in out["anchors"]} == {"FROM": "lingampally cross roads", "TO": "post office junction"}
    assert out["detour"] == {"towards": "tourist junction", "via": "barkatpura chaman", "origin": "kachiguda cross roads"}
    assert out["event_type"] == "DIVERSION"


def test_time_window_and_scope():
    out = ir.regex_ir("heavy vehicles and buses not allowed between tank bund road and necklace road from 6 pm to 10 pm")
    assert out["time_window"] == {"start": "6 pm", "end": "10 pm"}
    assert set(out["vehicle_scope"]) == {"HCV", "BUS"}


@pytest.mark.parametrize("text", ["gold prices dip in hyderabad", "police conducted a drive against drunk driving", "", "traffic police wish you a happy diwali"])
def test_non_advisories_yield_nothing(text):
    assert ir.regex_ir(text) is None


def test_confidence_grows_with_detail():
    one = ir.regex_ir("waterlogging at tolichowki underpass")
    three = ir.regex_ir("traffic diverted from jubilee hills check post towards madhapur via road no 36 due to waterlogging")
    assert 0.5 < one["confidence"] < three["confidence"] <= 0.95


@pytest.mark.parametrize("bad", [
    {"event_type": "NOPE", "anchors": [{"text": "x", "role": "AT"}], "cause": "VIP", "vehicle_scope": ["ALL"], "confidence": 0.5},
    {"event_type": "CLOSURE", "anchors": [], "cause": "VIP", "vehicle_scope": ["ALL"], "confidence": 0.5},
    {"event_type": "CLOSURE", "anchors": [{"text": "x", "role": "NEAR"}], "cause": "VIP", "vehicle_scope": ["ALL"], "confidence": 0.5},
    {"event_type": "CLOSURE", "anchors": [{"text": "x", "role": "AT"}], "cause": "VIP", "vehicle_scope": ["ALL"], "confidence": 3},
    {"anchors": [{"text": "x", "role": "AT"}]},
])
def test_validate_rejects_malformed_model_output(bad):
    assert not ir.validate_ir(bad)


def test_llm_path_is_off_without_a_server(monkeypatch):
    monkeypatch.setattr(ir, "LLM_BASE_URL", "")
    assert ir.llm_ir("anything") is None
