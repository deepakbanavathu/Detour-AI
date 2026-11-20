"""Database side of the commuter channel: identities, conversation state, subscriptions and corridors."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from .. import profiles, routing
from ..config import PII_KEY, PII_PEPPER
from . import chat

PROFILE_CODES = {"2W": ["2W"], "4W": ["4W_HATCH"], "BOTH": ["2W", "4W_HATCH"]}


def ident(channel: str, raw_id: str) -> bytes:
    """Stable, non-reversible id for a chat: the database never needs the phone number to recognise a sender."""
    return hmac.new(PII_PEPPER.encode(), f"{channel}:{raw_id}".encode(), hashlib.sha256).digest()


def get_state(c, channel: str, idh: bytes) -> dict | None:
    r = c.execute("SELECT step, data FROM client.chat_state WHERE channel = %s AND channel_id_hmac = %s", (channel, idh)).fetchone()
    return {"step": r["step"], "data": r["data"]} if r else None


def set_state(c, channel: str, idh: bytes, state: dict) -> None:
    c.execute(
        """
        INSERT INTO client.chat_state (channel, channel_id_hmac, step, data) VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (channel, channel_id_hmac) DO UPDATE SET step = EXCLUDED.step, data = EXCLUDED.data, updated_at = now()
        """,
        (channel, idh, state["step"], json.dumps(state["data"])))


def _pin(c, channel: str, idh: bytes, col: str) -> tuple[float, float] | None:
    r = c.execute(f"SELECT pgp_sym_decrypt({col}, %s) AS v FROM client.chat_state WHERE channel = %s AND channel_id_hmac = %s AND {col} IS NOT NULL",
                  (PII_KEY, channel, idh)).fetchone()
    if not r:
        return None
    lat, lng = r["v"].split(",")
    return float(lat), float(lng)


def _hold(c, channel: str, idh: bytes, col: str, lat: float, lng: float) -> None:
    c.execute(
        f"""
        INSERT INTO client.chat_state (channel, channel_id_hmac, step, {col}) VALUES (%s, %s, 'HOME', pgp_sym_encrypt(%s, %s))
        ON CONFLICT (channel, channel_id_hmac) DO UPDATE SET {col} = EXCLUDED.{col}
        """,
        (channel, idh, f"{lat:.6f},{lng:.6f}", PII_KEY))


def _sub_id(c, channel: str, idh: bytes) -> int | None:
    r = c.execute("SELECT sub_id FROM client.subscriber WHERE channel = %s AND channel_id_hmac = %s", (channel, idh)).fetchone()
    return int(r["sub_id"]) if r else None


def _build_corridors(c, sub_id: int, profile: str, home: tuple[float, float], work: tuple[float, float], windows: list[tuple[int, int]]) -> int:
    """One corridor per direction that has a window: morning = home to work, evening = work to home."""
    c.execute("DELETE FROM client.corridor WHERE sub_id = %s", (sub_id,))
    legs = []
    if any(lo < 720 for lo, _ in windows):
        legs.append(("home-work", home, work, [w for w in windows if w[0] < 720]))
    if any(lo >= 720 for lo, _ in windows):
        legs.append(("work-home", work, home, [w for w in windows if w[0] >= 720]))
    made = 0
    for label, o, d, wins in legs:
        edge_ids: list[int] = []
        for code in PROFILE_CODES[profile]:
            edge_ids += [e["edge_id"] for e in routing.route(profiles.get(code), o, d).edges]      # NoRoute propagates
        c.execute(
            """
            INSERT INTO client.corridor (sub_id, label, origin_enc, dest_enc, bearing_deg, cells_r9, windows)
            SELECT %(sub)s, %(label)s, pgp_sym_encrypt(%(o)s, %(key)s), pgp_sym_encrypt(%(d)s, %(key)s),
                   COALESCE(degrees(ST_Azimuth(ST_SetSRID(ST_MakePoint(%(olng)s, %(olat)s), 4326)::geography,
                                               ST_SetSRID(ST_MakePoint(%(dlng)s, %(dlat)s), 4326)::geography)), 0),
                   (SELECT array_agg(DISTINCT k) FROM (SELECT unnest(e.h3_r9) AS cell FROM net.edge e WHERE e.edge_id = ANY(%(edges)s)) x,
                           LATERAL h3_grid_disk(x.cell, 1) AS k),
                   %(wins)s::int4range[]
            """,
            {"sub": sub_id, "label": label, "o": f"{o[0]:.6f},{o[1]:.6f}", "d": f"{d[0]:.6f},{d[1]:.6f}", "key": PII_KEY,
             "olat": o[0], "olng": o[1], "dlat": d[0], "dlng": d[1], "edges": edge_ids,
             "wins": "{" + ",".join(f'"[{lo},{hi})"' for lo, hi in wins) + "}"})
        made += 1
    return made


def apply(c, channel: str, raw_id: str, idh: bytes, state: dict, effects: list[dict]) -> tuple[dict, list[dict]]:
    """Carry out the effects chat.step asked for. May change the state and add replies (same pin twice, no road found)."""
    extra: list[dict] = []
    for e in effects:
        do = e["do"]
        if do == "consent":
            state["data"]["consent_at"] = datetime.now(timezone.utc).isoformat()
            state["data"]["consent_version"] = e["version"]
            c.execute("UPDATE client.subscriber SET consent_at = now(), consent_text_version = %s WHERE channel = %s AND channel_id_hmac = %s",
                      (e["version"], channel, idh))                # a re-consent by an existing subscriber
        elif do == "hold_home":
            _hold(c, channel, idh, "home_enc", e["lat"], e["lng"])
        elif do == "hold_work":
            home = _pin(c, channel, idh, "home_enc")
            if home is None:
                state["step"] = "HOME"
                extra.append(chat.ASK_HOME)
            elif chat._dist_m(home, (e["lat"], e["lng"])) < e["min_trip_m"]:
                state["step"] = "WORK"
                extra.append(chat.ask_location("That is the same place as the start. Share the pin where your trip ends."))
            else:
                _hold(c, channel, idh, "work_enc", e["lat"], e["lng"])
        elif do == "subscribe":
            home, work = _pin(c, channel, idh, "home_enc"), _pin(c, channel, idh, "work_enc")
            if home is None or work is None:
                state["step"] = "HOME"
                extra.append(chat.ASK_HOME)
                continue
            sub = c.execute(
                """
                INSERT INTO client.subscriber (channel, channel_id_hmac, channel_id_enc, profile, consent_at, consent_text_version, age_confirmed, last_inbound_at)
                VALUES (%s, %s, pgp_sym_encrypt(%s, %s), %s, %s, %s, true, now())
                ON CONFLICT (channel, channel_id_hmac) DO UPDATE SET profile = EXCLUDED.profile, stopped_at = NULL,
                  consent_at = EXCLUDED.consent_at, consent_text_version = EXCLUDED.consent_text_version, channel_id_enc = EXCLUDED.channel_id_enc
                RETURNING sub_id
                """,
                (channel, idh, raw_id, PII_KEY, e["profile"], state["data"].get("consent_at") or datetime.now(timezone.utc).isoformat(),
                 state["data"].get("consent_version", chat.CONSENT_VERSION))).fetchone()["sub_id"]
            try:
                _build_corridors(c, sub, e["profile"], home, work, e["windows"])
                c.execute("UPDATE client.chat_state SET home_enc = NULL, work_enc = NULL WHERE channel = %s AND channel_id_hmac = %s", (channel, idh))
            except routing.NoRoute:
                c.execute("UPDATE client.subscriber SET stopped_at = now() WHERE sub_id = %s", (sub,))
                state["step"] = "HOME"
                extra[:] = [chat.ask_location("I could not find a road between those two pins for your vehicle. Share the start of your trip again, a little closer to a main road.")]
        elif do == "report":
            from . import reports                                   # here to avoid an import cycle with fanout
            sid = _sub_id(c, channel, idh)
            if sid is None:
                extra.append(chat.text("Finish setting up your route first, then you can report."))
                continue
            res = reports.create(c, sid, e["kind"], e["lat"], e["lng"])
            if not res["ok"]:
                extra.append(chat.text("That pin is not on a road I know. Send REPORT again and share a pin on the road itself." if res["reason"] == "no_road"
                                       else "You have reached today's limit for reports. Thank you for the ones you sent."))
            elif res["merged"]:
                extra.append(chat.text(f"Thanks. That matches a report already on {res['road']}, so it now counts as confirmed."))
            else:
                extra.append(chat.text(f"Noted on {res['road']}. Thank you. People whose route passes there will be asked to confirm it."))
        elif do == "confirm":
            from . import reports
            sid = _sub_id(c, channel, idh)
            if sid is not None:
                reports.vote(c, e["zone"], sid, e["yes"])
        elif do == "stop":
            c.execute("UPDATE client.subscriber SET stopped_at = now() WHERE channel = %s AND channel_id_hmac = %s", (channel, idh))
        elif do == "resume":
            c.execute("UPDATE client.subscriber SET stopped_at = NULL WHERE channel = %s AND channel_id_hmac = %s", (channel, idh))
        elif do == "clear_corridors":
            sid = _sub_id(c, channel, idh)
            if sid:
                c.execute("DELETE FROM client.corridor WHERE sub_id = %s", (sid,))
        elif do == "delete":
            c.execute("DELETE FROM client.subscriber WHERE channel = %s AND channel_id_hmac = %s", (channel, idh))      # corridors, alerts, outbox cascade
            c.execute("DELETE FROM client.chat_state WHERE channel = %s AND channel_id_hmac = %s", (channel, idh))
            return {"step": "NEW", "data": {}}, extra
    return state, extra


def handle_inbound(c, channel: str, raw_id: str, msg: dict) -> list[dict]:
    """One inbound message -> the replies to send. Commits its own work."""
    idh = ident(channel, raw_id)
    state, replies, effects = chat.step(get_state(c, channel, idh), msg)
    state, extra = apply(c, channel, raw_id, idh, state, effects)
    deleted = any(e["do"] == "delete" for e in effects)
    if not deleted:
        set_state(c, channel, idh, state)
        c.execute("UPDATE client.subscriber SET last_inbound_at = now() WHERE channel = %s AND channel_id_hmac = %s", (channel, idh))
    c.commit()
    # when an effect had to correct the conversation, its reply replaces the optimistic one
    return extra if extra else replies
