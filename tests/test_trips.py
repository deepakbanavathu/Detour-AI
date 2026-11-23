"""Live route pages for subscribers: the notice, tokens, redirects, the alert's buttons and the ROUTE command."""
from app import maps, packs
from app.channels import chat, fanout, telegram, whatsapp

Z1 = {"cause": "Waterlogging", "where": "at Tolichowki Underpass", "until": "9:40 am"}
Z2 = {"cause": "VIP movement", "where": "between A and B", "until": "12:00 pm"}
BASE = "https://d.example.in"
DONE = {"step": "DONE", "data": {"profile": "2W", "windows": "both", "consent_version": chat.CONSENT_VERSION}}


def test_notice_says_why_today_differs():
    assert packs.notice_text([], 0) is None
    assert packs.notice_text([Z1], 6) == "Waterlogging at Tolichowki Underpass, until about 9:40 am. Route A goes round it, about 6 min longer than usual."
    assert packs.notice_text([Z1], 1).endswith("Route A goes round it, in about the usual time.")
    assert packs.notice_text([Z1, Z2], 14) == "2 closures on your usual road, the first until about 9:40 am. Route A goes round them, about 14 min longer than usual."
    assert packs.notice_text([Z1], None).endswith("No way round was found for this vehicle.")


def test_trip_tokens_are_32_lowercase_hex_and_nothing_else():
    assert packs.valid_token("0123456789abcdef0123456789abcdef")
    for bad in ("", None, "0123456789ABCDEF0123456789ABCDEF", "0123456789abcdef0123456789abcde", "0123456789abcdef0123456789abcdef0", "../../etc/passwd", "g" * 32):
        assert not packs.valid_token(bad)
    assert packs.TRIP_PROFILE == {"2W": "2W", "4W": "4W_HATCH", "BOTH": "2W"}


def test_short_links_may_open_this_servers_own_live_route_and_no_one_elses():
    token = "0123456789abcdef0123456789abcdef"
    assert maps.redirect_kind(f"{BASE}/go/{token}", BASE) == "card"
    assert maps.redirect_kind(f"{BASE}/go/{token}", "") is None                     # no public address configured: never
    assert maps.redirect_kind(f"https://evil.example/go/{token}", BASE) is None
    assert maps.redirect_kind(f"{BASE}.evil.example/go/{token}", BASE) is None
    assert maps.redirect_kind(f"{BASE}/console", BASE) is None
    assert maps.redirect_kind(maps.waze_url((17.4, 78.4)), BASE) == "waze"


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "send", lambda payload, *a, **k: (sent.append(("tg", payload)) or ("1", None)))
    monkeypatch.setattr(whatsapp, "send", lambda payload, *a, **k: (sent.append(("wa", payload)) or ("1", None)))
    monkeypatch.setattr(fanout, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(fanout, "WHATSAPP_ALERT_TEMPLATE", "corridor_alert_v1")
    return sent


def test_alert_opens_the_live_route_first_on_each_channel(monkeypatch):
    sent = _capture(monkeypatch)
    args = ("body text", f"{BASE}/r/maps1", "maps1", ["a", "b", "c", "d", "e"])
    fanout._send("TELEGRAM", "55", *args, True, f"{BASE}/r/waze1", f"{BASE}/r/card1", "card1")
    assert [(r[0]["text"], r[0]["url"]) for r in sent[-1][1]["reply_markup"]["inline_keyboard"]] == [
        ("Open live route", f"{BASE}/r/card1"), ("Open in Google Maps", f"{BASE}/r/maps1"), ("Open in Waze", f"{BASE}/r/waze1")]

    fanout._send("WHATSAPP", "9198", *args, True, f"{BASE}/r/waze1", f"{BASE}/r/card1", "card1")          # inside the 24 h window
    inter = sent[-1][1]["interactive"]
    assert inter["action"]["parameters"] == {"display_text": "Open live route", "url": f"{BASE}/r/card1"}
    assert f"Google Maps: {BASE}/r/maps1" in inter["body"]["text"] and f"Waze: {BASE}/r/waze1" in inter["body"]["text"]

    fanout._send("WHATSAPP", "9198", *args, False, f"{BASE}/r/waze1", f"{BASE}/r/card1", "card1")         # outside it: the template's button
    assert sent[-1][1]["template"]["components"][1]["parameters"] == [{"type": "text", "text": "card1"}]


def test_without_a_public_address_the_maps_link_stays_the_button(monkeypatch):
    sent = _capture(monkeypatch)
    fanout._send("WHATSAPP", "9198", "body", "https://www.google.com/maps/dir/?api=1", "t", ["a"] * 5, True, None, None, None)
    assert sent[-1][1]["interactive"]["action"]["parameters"]["display_text"] == "Open in Google Maps"
    fanout._send("TELEGRAM", "55", "body", "https://www.google.com/maps/dir/?api=1", "t", ["a"] * 5, True, None, None, None)
    assert [r[0]["text"] for r in sent[-1][1]["reply_markup"]["inline_keyboard"]] == ["Open in Google Maps"]


def test_route_command_asks_the_store_for_links_once_set_up():
    for word in ("route", "ROUTES", "map", "Open"):
        s, replies, effects = chat.step(DONE, {"kind": "text", "text": word})
        assert s == DONE and replies == [] and effects == [{"do": "links"}]
    s, replies, effects = chat.step({"step": "HOME", "data": {"profile": "2W"}}, {"kind": "text", "text": "route"})
    assert effects == [] and "once your route is set up" in replies[0]["text"]
    assert "ROUTE" in chat.HELP
