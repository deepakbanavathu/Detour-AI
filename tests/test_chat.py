"""The onboarding conversation: every path a person can take through it."""
import pytest

from app.channels import chat


def t(text):
    return {"kind": "text", "text": text}


def b(i):
    return {"kind": "button", "id": i}


def loc(lat, lng):
    return {"kind": "location", "lat": lat, "lng": lng}


def run(msgs, state=None):
    """Feed messages through; return (state, last replies, all effects)."""
    effects, replies = [], []
    for m in msgs:
        state, replies, e = chat.step(state, m)
        effects += e
    return state, replies, effects


HAPPY = [t("hi"), b("consent_yes"), b("veh_2w"), loc(17.4486, 78.3908), loc(17.4126, 78.4480), b("win_both")]


def test_happy_path_ends_subscribed_with_both_windows():
    state, replies, effects = run(HAPPY)
    assert state == {"step": "DONE", "data": {"profile": "2W", "windows": "both", "consent_version": chat.CONSENT_VERSION}}
    assert [e["do"] for e in effects] == ["consent", "hold_home", "hold_work", "subscribe"]
    assert effects[-1] == {"do": "subscribe", "profile": "2W", "windows": [(420, 600), (1020, 1260)]}
    assert "You are set" in replies[0]["text"]


def test_first_message_shows_the_notice_before_anything_is_kept():
    state, replies, effects = chat.step(None, t("hello"))
    assert state["step"] == "CONSENT" and effects == []
    r = replies[0]
    assert r["type"] == "buttons" and [i for i, _ in r["buttons"]] == ["consent_yes", "consent_no"]
    for must in ("start and end pins", "does not track", "REPORT, the pin you share", "STOP", "DELETE", "18 or older", "DetourAI"):
        assert must in r["text"]


def test_declining_consent_keeps_nothing():
    state, replies, effects = run([t("hi"), b("consent_no")])
    assert state == {"step": "NEW", "data": {}} and effects == [] and "Nothing was saved" in replies[0]["text"]


def test_typed_answers_work_as_well_as_buttons():
    state, _, effects = run([t("hi"), t("yes"), t("I ride a scooter"), loc(17.44, 78.39), loc(17.41, 78.44), t("morning")])
    assert state["data"] == {"profile": "2W", "windows": "am", "consent_version": chat.CONSENT_VERSION} and effects[-1]["windows"] == [(420, 600)]
    state, _, _ = run([t("hi"), t("Yes"), t("car")])
    assert state["data"]["profile"] == "4W" and state["step"] == "HOME"


@pytest.mark.parametrize("upto,expect_type", [(2, "buttons"), (3, "ask_location"), (4, "ask_location"), (5, "buttons")])
def test_unexpected_input_re_asks_the_same_question(upto, expect_type):
    state, _, _ = run(HAPPY[:upto])
    before = state["step"]
    state, replies, effects = chat.step(state, t("what?"))
    assert state["step"] == before and effects == [] and replies[0]["type"] == expect_type


def test_text_instead_of_a_pin_explains_what_is_needed():
    state, _, _ = run(HAPPY[:3])
    _, replies, _ = chat.step(state, t("Madhapur"))
    assert "location pin" in replies[0]["text"]


@pytest.mark.parametrize("n", range(1, 7))
def test_stop_works_at_every_step(n):
    state, _, _ = run(HAPPY[:n])
    state, replies, effects = chat.step(state, t("stop"))
    assert state["step"] == "STOPPED" and effects == [{"do": "stop"}] and "Alerts are off" in replies[0]["text"]


@pytest.mark.parametrize("n", range(1, 7))
def test_delete_works_at_every_step_and_resets(n):
    state, _, _ = run(HAPPY[:n])
    state, replies, effects = chat.step(state, t("DELETE"))
    assert state == {"step": "NEW", "data": {}} and effects == [{"do": "delete"}] and "Erased" in replies[0]["text"]


def test_start_after_stop_resumes_a_finished_setup_and_restarts_an_unfinished_one():
    state, _, _ = run(HAPPY + [t("STOP")])
    state, replies, effects = chat.step(state, t("start"))
    assert state["step"] == "DONE" and effects == [{"do": "resume"}]
    state, _, _ = run(HAPPY[:3] + [t("STOP")])
    state, replies, effects = chat.step(state, t("START"))
    assert state["step"] == "CONSENT" and effects == [] and replies[0]["type"] == "buttons"


def test_show_describes_what_is_kept_without_revealing_pins():
    state, _, _ = run(HAPPY)
    _, replies, effects = chat.step(state, t("show"))
    text = replies[0]["text"]
    assert effects == [] and "two-wheeler" in text and "mornings" in text and "encrypted" in text
    assert "17.4" not in text and "78.3" not in text
    _, replies, _ = chat.step(None, t("SHOW"))
    assert replies[0]["text"] == "Nothing is kept for this chat yet."


def test_reset_keeps_consent_and_asks_for_the_route_again():
    state, _, _ = run(HAPPY)
    state, replies, effects = chat.step(state, t("reset"))
    assert state["step"] == "VEHICLE" and effects == [{"do": "clear_corridors"}] and replies[0] is chat.ASK_VEHICLE


def test_done_state_answers_anything_with_short_help():
    state, _, _ = run(HAPPY)
    state2, replies, effects = chat.step(state, t("thanks"))
    assert state2 == state and effects == [] and "STOP" in replies[0]["text"]
    _, replies, _ = chat.step(state, t("help"))
    assert replies[0]["text"] == chat.HELP


def test_buttons_respect_whatsapp_limits():
    for r in (chat.ASK_CONSENT, chat.ASK_VEHICLE, chat.ASK_WINDOWS, chat.ASK_REPORT_KIND, chat.ASK_REPORT_CONSENT):
        assert len(r["buttons"]) <= 3 and all(len(title) <= 20 for _, title in r["buttons"]) and len(r["text"]) <= 1024


def test_distance_helper():
    assert 6000 < chat._dist_m((17.4486, 78.3908), (17.4126, 78.4480)) < 8000
    assert chat._dist_m((17.4486, 78.3908), (17.4487, 78.3909)) < chat.MIN_TRIP_M
