"""Channel adapters and alert wording: payload shapes, webhook parsing, signatures, windows."""
import hashlib
import hmac
import json

from app.channels import alerts, chat, sms, store, telegram, whatsapp

# ---------------------------------------------------------------- WhatsApp


def test_signature_accepts_only_the_right_secret_and_body():
    raw = b'{"object":"whatsapp_business_account"}'
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(raw, sig, "s3cret")
    assert not whatsapp.verify_signature(raw + b" ", sig, "s3cret")
    assert not whatsapp.verify_signature(raw, sig, "other")
    assert not whatsapp.verify_signature(raw, None, "s3cret") and not whatsapp.verify_signature(raw, sig, "")
    assert not whatsapp.verify_signature(raw, sig.replace("sha256=", "sha1="), "s3cret")


WEBHOOK = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
    "messaging_product": "whatsapp", "contacts": [{"wa_id": "919800000001", "profile": {"name": "R"}}],
    "messages": [
        {"from": "919800000001", "id": "wamid.1", "type": "text", "text": {"body": "hi"}},
        {"from": "919800000001", "id": "wamid.2", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "veh_2w", "title": "Two-wheeler"}}},
        {"from": "919800000001", "id": "wamid.3", "type": "location", "location": {"latitude": 17.4486, "longitude": 78.3908}},
        {"from": "919800000001", "id": "wamid.4", "type": "image", "image": {"id": "x"}}],
    "statuses": [{"id": "wamid.out1", "status": "delivered", "recipient_id": "919800000001"},
                 {"id": "wamid.out2", "status": "failed", "recipient_id": "919800000002", "errors": [{"code": 131026, "title": "Message undeliverable"}]}]}}]}]}


def test_webhook_parse_messages_and_statuses():
    msgs, statuses = whatsapp.parse_webhook(WEBHOOK)
    assert [(m["kind"], m["msg_id"]) for m in msgs] == [("text", "wamid.1"), ("button", "wamid.2"), ("location", "wamid.3")]     # image skipped
    assert msgs[0]["text"] == "hi" and msgs[1]["id"] == "veh_2w" and (msgs[2]["lat"], msgs[2]["lng"]) == (17.4486, 78.3908)
    assert statuses == [{"id": "wamid.out1", "status": "DELIVERED", "recipient": "919800000001", "error": None},
                        {"id": "wamid.out2", "status": "FAILED", "recipient": "919800000002", "error": "Message undeliverable"}]
    assert whatsapp.parse_webhook({}) == ([], [])


def test_button_reply_id_is_what_the_state_machine_reads():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "91", "id": "w", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "consent_yes", "title": "Yes, send alerts"}}}]}}]}]}
    m = whatsapp.parse_webhook(payload)[0][0]
    # the provider's message id (used to drop redeliveries) and the button's reply id must never share a key:
    # two people pressing the same button would otherwise look like one duplicate
    assert m["kind"] == "button" and m["id"] == "consent_yes" and m["msg_id"] == "w"
    state, _, _ = chat.step({"step": "CONSENT", "data": {}}, m)
    assert state["step"] == "VEHICLE"


def test_render_each_reply_type():
    to = "919800000001"
    txt = whatsapp.render(chat.text("hello"), to)
    assert txt == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text", "text": {"body": "hello", "preview_url": False}}
    btn = whatsapp.render(chat.ASK_VEHICLE, to)["interactive"]
    assert btn["type"] == "button" and [x["reply"]["id"] for x in btn["action"]["buttons"]] == ["veh_2w", "veh_4w", "veh_both"]
    locq = whatsapp.render(chat.ASK_HOME, to)["interactive"]
    assert locq["type"] == "location_request_message" and locq["action"] == {"name": "send_location"}


def test_template_and_link_messages():
    tpl = whatsapp.template_message("91", "corridor_alert_v1", ["Waterlogging", "A to B", "9:40 am", "Road No 36", "morning"], "tok123")["template"]
    assert tpl["name"] == "corridor_alert_v1" and tpl["language"] == {"code": "en"}
    assert [p["text"] for p in tpl["components"][0]["parameters"]] == ["Waterlogging", "A to B", "9:40 am", "Road No 36", "morning"]
    assert tpl["components"][1] == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "tok123"}]}
    assert len(whatsapp.template_message("91", "cleared_v1", ["Waterlogging", "A to B"], None)["template"]["components"]) == 1
    link = whatsapp.link_message("91", "body", "Open in Google Maps", "https://www.google.com/maps/dir/?api=1")["interactive"]
    assert link["type"] == "cta_url" and link["action"]["parameters"] == {"display_text": "Open in Google Maps", "url": "https://www.google.com/maps/dir/?api=1"}


def test_send_without_credentials_sends_nothing(monkeypatch):
    monkeypatch.setattr(whatsapp, "WHATSAPP_TOKEN", "")
    assert whatsapp.send({"x": 1}) == (None, "WhatsApp is not configured")
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "")
    assert telegram.send({"x": 1}) == (None, "Telegram is not configured")
    monkeypatch.setattr(sms, "SMS_PROVIDER", "")
    assert sms.send("9198", "x") == (None, "SMS is not configured") and not sms.configured()
    monkeypatch.setattr(sms, "SMS_PROVIDER", "log")
    assert sms.send("9198", "x") == ("log", None) and sms.configured()


# ---------------------------------------------------------------- Telegram

def test_telegram_updates():
    assert telegram.parse_update({"message": {"chat": {"id": 55}, "text": "/start"}}) == {"from": "55", "kind": "text", "text": "hi"}
    assert telegram.parse_update({"message": {"chat": {"id": 55}, "text": "/stop"}})["text"] == "stop"
    assert telegram.parse_update({"callback_query": {"id": "cb1", "data": "veh_4w", "message": {"chat": {"id": 55}}}}) == \
        {"from": "55", "kind": "button", "id": "veh_4w", "callback_id": "cb1"}
    p = telegram.parse_update({"message": {"chat": {"id": 55}, "location": {"latitude": 17.4, "longitude": 78.4}}})
    assert (p["kind"], p["lat"], p["lng"], p["live"]) == ("location", 17.4, 78.4, False)
    assert telegram.parse_update({"edited_message": {"chat": {"id": 55}, "location": {"latitude": 17.4, "longitude": 78.4}}})["live"] is True
    assert telegram.parse_update({"message": {"chat": {"id": 55}, "sticker": {}}}) is None and telegram.parse_update({}) is None


def test_telegram_render():
    b = telegram.render(chat.ASK_WINDOWS, "55")
    assert b["chat_id"] == "55" and [row[0]["callback_data"] for row in b["reply_markup"]["inline_keyboard"]] == ["win_both", "win_am", "win_pm"]
    assert telegram.render(chat.ASK_HOME, "55")["reply_markup"]["keyboard"][0][0]["request_location"] is True
    assert telegram.render(chat.text("ok"), "55")["reply_markup"] == {"remove_keyboard": True}
    assert telegram.link_message("55", "body", "Open in Google Maps", "https://x")["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://x"


# ---------------------------------------------------------------- alert wording and timing

def test_window_includes_the_half_hour_before_it_opens():
    wins = [(420, 600), (1020, 1260)]
    assert alerts.in_window(wins, 390) and alerts.in_window(wins, 420) and alerts.in_window(wins, 599)
    assert not alerts.in_window(wins, 389) and not alerts.in_window(wins, 600) and not alerts.in_window(wins, 800)
    assert alerts.in_window(wins, 990) and not alerts.in_window([], 500)


def test_alert_text_reads_as_a_status_update_not_a_promotion():
    txt = alerts.alert_text("WATERLOGGING", "Jubilee Hills Check Post to Madhapur", "9:40 am", ["Road No 36", "Road No 45"], "morning", 12)
    assert txt.splitlines() == ["Waterlogging: Jubilee Hills Check Post to Madhapur.", "It touches your morning trip, until about 9:40 am.",
                                "Suggested way round: via Road No 36, Road No 45 (about 12 min longer).", "Suggested only: follow signs and police on the road."]
    for word in ("offer", "free", "discount", "buy", "!"):
        assert word not in txt.lower()
    assert "Suggested way round" not in alerts.alert_text("VIP", "at X", "1 pm", [], "evening", None)


def test_template_params_have_no_newlines_or_blanks():
    p = alerts.template_params("ROADWORK", "A\nto B", "6:00 pm", [], "evening")
    assert p == ["Road work", "A to B", "6:00 pm", "local roads", "evening"] and all("\n" not in x and x for x in p)


def test_sms_stands_alone_and_stays_short():
    s = alerts.sms_text("WATERLOGGING", "Jubilee Hills Check Post to Madhapur", ["Road No 36", "Road No 45", "KBR Park Road"], "https://d.example.in/r/abc123")
    assert s == "DetourAI: Waterlogging Jubilee Hills Check Post to Madhapur. Go via Road No 36, Road No 45. https://d.example.in/r/abc123"
    assert len(s) <= 300 and alerts.sms_text("VIP", "at X", [], None) == "DetourAI: VIP movement at X. Go via another road."


def test_cleared_and_trip_label():
    assert alerts.cleared_text("WATERLOGGING", "at Tolichowki Underpass") == "Cleared: waterlogging at Tolichowki Underpass. Your usual route is open again."
    assert (alerts.trip_label("home-work"), alerts.trip_label("work-home"), alerts.trip_label("custom-1")) == ("morning", "evening", "regular")


# ---------------------------------------------------------------- identity

def test_chat_identity_is_stable_per_channel_and_not_the_number():
    a = store.ident("WHATSAPP", "919800000001")
    assert a == store.ident("WHATSAPP", "919800000001") and len(a) == 32
    assert a != store.ident("TELEGRAM", "919800000001") and a != store.ident("WHATSAPP", "919800000002")
    assert b"9198" not in a


def test_webhook_fixture_is_valid_json_roundtrip():
    assert json.loads(json.dumps(WEBHOOK))["entry"][0]["changes"][0]["field"] == "messages"
