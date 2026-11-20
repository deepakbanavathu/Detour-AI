"""Crowd reports: trust, starting confidence, what votes do, and the REPORT conversation."""
import pytest

from app.channels import chat, reports

DONE = {"step": "DONE", "data": {"profile": "2W", "windows": "both", "consent_version": chat.CONSENT_VERSION}}
OLD = {"step": "DONE", "data": {"profile": "2W", "windows": "both", "consent_version": "2026-11-v1"}}      # agreed before reports existed


def t(text):
    return {"kind": "text", "text": text}


def b(i):
    return {"kind": "button", "id": i}


def test_a_new_reporter_starts_a_candidate_and_a_proven_one_starts_active():
    assert reports.start_confidence(0.5) == 0.6 and reports.start_confidence(0.9) == 0.8
    assert reports.start_confidence(0.1) < 0.45 and reports.start_confidence(5) == 0.85


def test_trust_moves_with_outcomes_inside_bounds():
    assert reports.adjust_trust(0.5, "CONFIRMED") == 0.6 and reports.adjust_trust(0.5, "REFUTED") == 0.35
    assert reports.adjust_trust(0.5, "EXPIRED") == 0.47 and reports.adjust_trust(0.5, "PENDING") == 0.5
    assert reports.adjust_trust(0.93, "CONFIRMED") == 0.95 and reports.adjust_trust(0.12, "REFUTED") == 0.1


@pytest.mark.parametrize("origin,status,yes,no,expect", [
    ("CROWD", "CANDIDATE", 0.5, 0.0, "ACTIVATE"),        # one average person confirms
    ("CROWD", "CANDIDATE", 0.3, 0.0, "KEEP"),            # one low-trust person is not enough
    ("CROWD", "ACTIVE", 0.5, 1.5, "REFUTE"),             # two more average people say it is clear
    ("CROWD", "ACTIVE", 0.5, 1.0, "KEEP"),               # one against one: stays
    ("ADVISORY", "ACTIVE", 0.0, 1.0, "DECAY"),           # a police closure is shortened by the crowd, never removed
    ("FEED", "ACTIVE", 0.0, 2.5, "DECAY"),
    ("ADVISORY", "DECAYING", 1.0, 0.5, "ACTIVATE"),      # people say it is still blocked: back to active
    ("ADVISORY", "ACTIVE", 2.0, 0.0, "KEEP"),
])
def test_vote_effect(origin, status, yes, no, expect):
    assert reports.vote_effect(origin, status, yes, no) == expect


def test_report_kinds_have_short_lives_and_fit_buttons():
    assert {k: v[3] for k, v in reports.KIND.items()} == {"WATER": 120, "CLOSED": 180, "ACCIDENT": 45}
    q = reports.check_question("waterlogging", "at Road No 36", 12, crowd=True)
    assert [i for i, _ in q["buttons"]] == ["zc_y_12", "zc_n_12"] and all(len(title) <= 20 for _, title in q["buttons"])
    assert "Are you seeing it?" in q["text"]
    assert reports.check_question("x", "at Y", 7, crowd=False)["text"] == "Is it still blocked at Y?"


def test_report_flow_kind_then_pin_then_back():
    s, r, e = chat.step(DONE, t("report"))
    assert s["step"] == "REPORT_KIND" and r[0] is chat.ASK_REPORT_KIND and e == []
    s, r, e = chat.step(s, b("rep_water"))
    assert s["step"] == "REPORT_PIN" and r[0]["type"] == "ask_location"
    s, r, e = chat.step(s, {"kind": "location", "lat": 17.41, "lng": 78.43})
    assert s == DONE and r == [] and e == [{"do": "report", "kind": "WATER", "lat": 17.41, "lng": 78.43}]


def test_report_accepts_typed_kinds_and_re_asks_on_noise():
    s, _, _ = chat.step(DONE, t("REPORT"))
    s2, r, _ = chat.step(s, t("hmm"))
    assert s2["step"] == "REPORT_KIND" and r[0] is chat.ASK_REPORT_KIND
    s3, _, _ = chat.step(s, t("road is blocked"))
    assert s3["data"]["report"]["kind"] == "CLOSED"
    s4, r, e = chat.step(s3, t("it is here"))
    assert s4["step"] == "REPORT_PIN" and e == [] and r[0]["type"] == "ask_location"


def test_cancel_inside_a_report_goes_back_and_does_not_unsubscribe():
    s, _, _ = chat.step(DONE, t("report"))
    s, r, e = chat.step(s, t("cancel"))
    assert s == DONE and e == [] and r[0]["text"] == "Nothing was reported."
    s, r, e = chat.step(DONE, t("cancel"))                      # outside a report, CANCEL still stops alerts
    assert s["step"] == "STOPPED" and e == [{"do": "stop"}]


def test_stop_and_delete_work_in_the_middle_of_a_report():
    s, _, _ = chat.step(DONE, t("report"))
    s1, _, e1 = chat.step(s, t("STOP"))
    assert s1["step"] == "STOPPED" and e1 == [{"do": "stop"}] and "report" not in s1["data"]
    s2, _, e2 = chat.step(s, t("DELETE"))
    assert s2 == {"step": "NEW", "data": {}} and e2 == [{"do": "delete"}]


@pytest.mark.parametrize("state", [None, {"step": "CONSENT", "data": {}}, {"step": "HOME", "data": {"profile": "2W"}}, {"step": "STOPPED", "data": {}}])
def test_report_needs_a_finished_setup(state):
    s, r, e = chat.step(state, t("report"))
    assert e == [] and "Finish setting up your route first" in r[0]["text"] and s["step"] != "REPORT_KIND"


@pytest.mark.parametrize("state", [None, DONE, {"step": "HOME", "data": {"profile": "2W"}}, {"step": "REPORT_PIN", "data": {"report": {"back": "DONE", "kind": "WATER"}}}])
def test_still_blocked_answers_work_anywhere_and_leave_the_conversation_alone(state):
    before = None if state is None else {"step": state["step"], "data": dict(state["data"])}
    s, r, e = chat.step(state, b("zc_n_41"))
    assert e == [{"do": "confirm", "zone": 41, "yes": False}] and "Thanks" in r[0]["text"]
    assert s == (before or {"step": "NEW", "data": {}})
    _, _, e = chat.step(state, b("zc_y_41"))
    assert e == [{"do": "confirm", "zone": 41, "yes": True}]


def test_malformed_check_buttons_do_nothing():
    for bad in ("zc_", "zc_x_3", "zc_y_abc"):
        s, r, e = chat.step(DONE, b(bad))
        assert s == DONE and r == [] and e == []


def test_help_mentions_report():
    assert "REPORT" in chat.HELP


def test_people_who_agreed_before_reports_existed_are_asked_once():
    s, r, e = chat.step(OLD, t("report"))
    assert s["step"] == "REPORT_CONSENT" and r[0] is chat.ASK_REPORT_CONSENT and e == []
    assert "pin you share for a report is kept" in r[0]["text"]
    s2, r2, e2 = chat.step(s, b("rep_ok"))
    assert s2["step"] == "REPORT_KIND" and e2 == [{"do": "consent", "version": chat.CONSENT_VERSION}] and s2["data"]["consent_version"] == chat.CONSENT_VERSION
    s3, r3, e3 = chat.step(s2, b("rep_closed"))
    assert s3["step"] == "REPORT_PIN"
    s4, r4, e4 = chat.step(s, b("rep_no"))                        # declining leaves alerts as they were
    assert s4["step"] == "DONE" and e4 == [] and "report" not in s4["data"] and s4["data"]["consent_version"] == "2026-11-v1"
    s5, _, e5 = chat.step(s, t("STOP"))
    assert s5["step"] == "STOPPED" and e5 == [{"do": "stop"}]


def test_an_unproven_account_counts_for_less():
    assert reports.vote_weight(0.5, proven=False) == 0.3 and reports.vote_weight(0.2, proven=False) == 0.2
    assert reports.vote_weight(0.6, proven=True) == 0.6
    w = reports.vote_weight(0.5, False)
    assert reports.vote_effect("CROWD", "CANDIDATE", w, 0) == "KEEP" and reports.vote_effect("CROWD", "CANDIDATE", 2 * w, 0) == "ACTIVATE"      # two new accounts to confirm
    assert reports.vote_effect("CROWD", "ACTIVE", 0, 3 * w) == "KEEP" and reports.vote_effect("CROWD", "ACTIVE", 0, 4 * w) == "REFUTE"           # four to end
    assert reports.vote_effect("ADVISORY", "ACTIVE", 0, 4 * w) == "DECAY"
