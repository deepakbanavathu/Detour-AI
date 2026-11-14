"""The onboarding conversation, as a pure state machine shared by WhatsApp and Telegram.

step(state, msg) -> (state, replies, effects). Replies are channel-neutral; effects are what the caller must do
in the database (subscribe, build corridors, stop, resume, delete). Nothing here touches the network or the database.
"""
from __future__ import annotations

import math

CONSENT_VERSION = "2026-11-v1"
WINDOWS = {"both": [(420, 600), (1020, 1260)], "am": [(420, 600)], "pm": [(1020, 1260)]}     # minutes of day, IST
MIN_TRIP_M = 300.0

NOTICE = (
    "DetourAI messages you when a road closure or flooding touches your regular route in Hyderabad.\n\n"
    "To do that it keeps your start and end pins, your travel times, your vehicle type and the alerts it sent you. "
    "It does not track where you are.\n\n"
    "Reply STOP to end, DELETE to erase everything, SHOW to see what is kept. You must be 18 or older.\n\n"
    "Set up alerts from DetourAI on this chat?")
HELP = "Send SHOW to see what is kept, STOP to pause alerts, START to resume, RESET to change your route, DELETE to erase everything."


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def buttons(t: str, options: list[tuple[str, str]]) -> dict:
    assert len(options) <= 3 and all(len(title) <= 20 for _, title in options)
    return {"type": "buttons", "text": t, "buttons": options}


def ask_location(t: str) -> dict:
    return {"type": "ask_location", "text": t}


ASK_VEHICLE = buttons("What do you travel by?", [("veh_2w", "Two-wheeler"), ("veh_4w", "Car"), ("veh_both", "Both")])
ASK_HOME = ask_location("Where does your trip usually start? Share that place as a location pin.")
ASK_WORK = ask_location("And where does it end? Share that place as a location pin.")
ASK_WINDOWS = buttons("When do you travel?", [("win_both", "Morning and evening"), ("win_am", "Morning only"), ("win_pm", "Evening only")])
ASK_CONSENT = buttons(NOTICE, [("consent_yes", "Yes, send alerts"), ("consent_no", "No thanks")])


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    k = math.cos(math.radians(a[0])) * 111320
    return math.hypot((a[1] - b[1]) * k, (a[0] - b[0]) * 110540)


def _word(msg: dict) -> str:
    return (msg.get("text") or "").strip().upper()


def _pick(msg: dict, table: dict[str, str], words: dict[str, str]) -> str | None:
    if msg["kind"] == "button":
        return table.get(msg.get("id", ""))
    w = _word(msg)
    for key, val in words.items():
        if key in w:
            return val
    return None


def describe(data: dict, since: str | None = None) -> str:
    veh = {"2W": "two-wheeler", "4W": "car", "BOTH": "two-wheeler and car"}.get(data.get("profile"), "not set")
    win = {"both": "mornings (7 to 10) and evenings (5 to 9)", "am": "mornings (7 to 10)", "pm": "evenings (5 to 9)"}.get(data.get("windows"), "not set")
    return (f"Kept for this chat: a start pin and an end pin (stored encrypted), vehicle: {veh}, travel times: {win}, "
            f"and the alerts sent to you{' since ' + since if since else ''}. Send DELETE to erase all of it.")


def step(state: dict | None, msg: dict) -> tuple[dict, list[dict], list[dict]]:
    s = {"step": "NEW", "data": {}} if not state else {"step": state["step"], "data": dict(state.get("data") or {})}
    w = _word(msg) if msg["kind"] == "text" else ""

    # ---- commands that work at any point
    if w in ("STOP", "UNSUBSCRIBE", "CANCEL"):
        s["step"] = "STOPPED"
        return s, [text("Alerts are off. Send START to turn them back on, or DELETE to erase your data.")], [{"do": "stop"}]
    if w == "DELETE":
        return {"step": "NEW", "data": {}}, [text("Erased. Nothing about this chat is kept. Say hi if you want alerts again.")], [{"do": "delete"}]
    if w == "SHOW":
        return s, [text(describe(s["data"]) if s["data"] else "Nothing is kept for this chat yet.")], []
    if w == "HELP":
        return s, [text(HELP)], []
    if w == "RESET" and s["step"] in ("DONE", "STOPPED", "HOME", "WORK", "WINDOWS"):
        s = {"step": "VEHICLE", "data": {}}
        return s, [ASK_VEHICLE], [{"do": "clear_corridors"}]
    if w == "START" and s["step"] == "STOPPED":
        if s["data"].get("windows"):
            s["step"] = "DONE"
            return s, [text("Alerts are back on for your route.")], [{"do": "resume"}]
        s = {"step": "CONSENT", "data": {}}
        return s, [ASK_CONSENT], []

    st = s["step"]
    if st in ("NEW", "STOPPED"):
        s["step"] = "CONSENT"
        return s, [ASK_CONSENT], []

    if st == "CONSENT":
        choice = _pick(msg, {"consent_yes": "yes", "consent_no": "no"}, {"YES": "yes", "NO": "no"})
        if choice == "yes":
            s["step"] = "VEHICLE"
            return s, [ASK_VEHICLE], [{"do": "consent", "version": CONSENT_VERSION}]
        if choice == "no":
            return {"step": "NEW", "data": {}}, [text("No problem. Nothing was saved. Say hi if you change your mind.")], []
        return s, [ASK_CONSENT], []

    if st == "VEHICLE":
        prof = _pick(msg, {"veh_2w": "2W", "veh_4w": "4W", "veh_both": "BOTH"},
                     {"BOTH": "BOTH", "TWO": "2W", "BIKE": "2W", "SCOOT": "2W", "2W": "2W", "CAR": "4W", "4W": "4W"})
        if prof is None:
            return s, [ASK_VEHICLE], []
        s["data"]["profile"] = prof
        s["step"] = "HOME"
        return s, [ASK_HOME], []

    if st == "HOME":
        if msg["kind"] != "location":
            return s, [ask_location("I need a location pin for the start of your trip. Use the button, or attach a Location.")], []
        s["step"] = "WORK"
        return s, [ASK_WORK], [{"do": "hold_home", "lat": msg["lat"], "lng": msg["lng"]}]

    if st == "WORK":
        if msg["kind"] != "location":
            return s, [ask_location("I need a location pin for the end of your trip. Use the button, or attach a Location.")], []
        s["step"] = "WINDOWS"
        return s, [ASK_WINDOWS], [{"do": "hold_work", "lat": msg["lat"], "lng": msg["lng"], "min_trip_m": MIN_TRIP_M}]

    if st == "WINDOWS":
        win = _pick(msg, {"win_both": "both", "win_am": "am", "win_pm": "pm"}, {"BOTH": "both", "MORNING": "am", "EVENING": "pm"})
        if win is None:
            return s, [ASK_WINDOWS], []
        s["data"]["windows"] = win
        s["step"] = "DONE"
        done = text("You are set. I will message you only when a closure or flooding touches your route during your travel times, "
                    "with a link that opens the way round in Google Maps. " + HELP)
        return s, [done], [{"do": "subscribe", "profile": s["data"]["profile"], "windows": WINDOWS[win]}]

    # DONE: anything else gets the short help
    return s, [text("You are set; I only write when your route is affected. " + HELP)], []
