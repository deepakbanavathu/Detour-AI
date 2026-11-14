"""The road brief: today's zones and the team's regular routes, as JSON, WhatsApp text and email HTML."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from . import maps, profiles, routing
from .config import CITY_ID
from .db import conn

IST = timezone(timedelta(hours=5, minutes=30))
CAUSE = {"WATERLOGGING": "Waterlogging", "VIP": "VIP movement", "ROADWORK": "Road work", "ACCIDENT": "Accident",
         "PROCESSION": "Procession", "TREE_FALL": "Fallen tree", "GATE": "Gate closed", "UNKNOWN": "Restriction"}
VEHICLE = {"2W": "Two-wheeler", "3W": "Auto", "4W_HATCH": "Car", "4W_SUV": "SUV", "LCV": "Light truck", "HCV": "Truck", "BUS": "Bus"}
MASK_WORDS = [(1, "two-wheelers"), (2, "autos"), (12, "cars"), (16, "light trucks"), (32, "trucks"), (64, "buses")]
AFFECTED_MIN_S = 60          # a route counts as affected when it runs a minute or more over its usual time


def _clock(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%-I:%M %p").lower()


def _place_case(text: str) -> str:
    """'lb nagar junction' -> 'LB Nagar Junction'; short tokens are initials except a few common words."""
    keep = {"no", "to", "of", "at", "x"}
    return " ".join(w.upper() if len(w) <= 2 and w.isalpha() and w not in keep else w.capitalize() for w in text.split())


def _where(ir: dict | None, zone_id: int) -> str:
    a = {x["role"]: _place_case(x["text"]) for x in (ir or {}).get("anchors", [])}
    d = (ir or {}).get("detour") or {}
    if "FROM" in a and "TO" in a:
        via = a.get("VIA") or (_place_case(d["via"]) if d.get("via") else None)
        return f"{a['FROM']} to {a['TO']}" + (f", detour via {via}" if via else "")
    if "BETWEEN_A" in a and "BETWEEN_B" in a:
        return f"between {a['BETWEEN_A']} and {a['BETWEEN_B']}"
    for role in ("AT", "FROM", "TO"):
        if role in a:
            return f"at {a[role]}"
    return f"zone {zone_id}"


def _who(mask: int) -> str:
    if mask & 127 == 127:
        return "all vehicles"
    words = [w for bit, w in MASK_WORDS if mask & bit]
    return ", ".join(words) or "all vehicles"


def build(city_id: int = CITY_ID) -> dict:
    now = datetime.now(timezone.utc)
    with conn() as c:
        city = c.execute("SELECT name FROM geo.city WHERE city_id = %s", (city_id,)).fetchone()
        zrows = c.execute(
            """
            SELECT z.zone_id, z.cause, z.status, z.valid_to, z.profile_mask, z.corroborations, z.penalty_factor,
                   (SELECT a.ir FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id) AS ir
            FROM geo.zone_of_avoidance z
            WHERE z.city_id = %s AND z.status IN ('ACTIVE','DECAYING') AND z.valid_to > now()
            ORDER BY z.penalty_factor DESC, z.valid_to
            """,
            (city_id,),
        ).fetchall()
        rrows = c.execute(
            """
            SELECT route_id, name, profile, origin_label, dest_label,
                   ST_Y(origin) AS olat, ST_X(origin) AS olng, ST_Y(dest) AS dlat, ST_X(dest) AS dlng
            FROM client.team_route WHERE city_id = %s AND active ORDER BY name, profile
            """,
            (city_id,),
        ).fetchall()

    zones = [{
        "zone_id": z["zone_id"], "cause": CAUSE.get(z["cause"], "Restriction"), "where": _where(z["ir"], z["zone_id"]),
        "who": _who(int(z["profile_mask"])), "until": _clock(z["valid_to"]), "clearing": z["status"] == "DECAYING",
        "closed": float(z["penalty_factor"]) >= 1.0, "sources": int(z["corroborations"]),
    } for z in zrows]

    routes = []
    for r in rrows:
        item = {"route_id": r["route_id"], "name": r["name"], "vehicle": VEHICLE.get(r["profile"], r["profile"]),
                "from": r["origin_label"], "to": r["dest_label"]}
        try:
            p = profiles.get(r["profile"])
            o, d = (r["olat"], r["olng"]), (r["dlat"], r["dlng"])
            cur = routing.route(p, o, d)
            usual = routing.route(p, o, d, use_zones=False)
            delay = cur.duration_s - usual.duration_s
            rerouted = [e["edge_id"] for e in cur.edges] != [e["edge_id"] for e in usual.edges]
            names: dict[str, float] = {}
            for e in cur.edges:
                if e.get("name"):
                    names[e["name"]] = names.get(e["name"], 0.0) + float(e["length_m"])
            item.update({
                "minutes": round(cur.duration_s / 60), "km": round(cur.distance_m / 1000, 1),
                "usual_minutes": round(usual.duration_s / 60), "delay_minutes": max(0, round(delay / 60)),
                "affected": bool(rerouted and delay >= AFFECTED_MIN_S) or delay >= AFFECTED_MIN_S, "rerouted": rerouted,
                "via": sorted(names, key=names.get, reverse=True)[:3],
                "maps_url": maps.maps_url(p.code, d, maps.waypoints(cur.edges), o),
            })
        except (ValueError, routing.NoRoute) as e:
            item.update({"error": str(e), "affected": True})
        routes.append(item)

    routes.sort(key=lambda x: (not x.get("affected"), -x.get("delay_minutes", 0), x["name"]))
    local = now.astimezone(IST)
    return {
        "city": city["name"] if city else "City", "generated_at": now.isoformat(),
        "date_label": local.strftime("%A %-d %B"), "time_label": _clock(now),
        "zones": zones, "routes": routes,
        "summary": _summary(zones, routes),
    }


def _summary(zones: list[dict], routes: list[dict]) -> str:
    hit = [r for r in routes if r.get("affected")]
    if not zones:
        return "No diversions on record right now. Every saved route is on its usual road."
    z = f"{len(zones)} {'zone' if len(zones) == 1 else 'zones'} to avoid"
    if not routes:
        return f"{z}. No team routes saved yet."
    if not hit:
        return f"{z}; none of them touch your {len(routes)} saved {'route' if len(routes) == 1 else 'routes'}."
    worst = max(hit, key=lambda r: r.get("delay_minutes", 0))
    return (f"{z}. {len(hit)} of your {len(routes)} saved routes {'is' if len(hit) == 1 else 'are'} affected; "
            f"the worst is {worst['name']}, {worst.get('delay_minutes', 0)} min over its usual time.")


def as_text(b: dict) -> str:
    """WhatsApp-ready: *bold* markers, short lines, one link per affected route."""
    out = [f"*{b['city']} road brief*", f"{b['date_label']}, {b['time_label']}", "", b["summary"], ""]
    if b["zones"]:
        out.append(f"*Avoid ({len(b['zones'])})*")
        for z in b["zones"]:
            state = "Clearing, until" if z["clearing"] else "Until"
            out.append(f"• {z['cause']}: {z['where']}. {state} about {z['until']}" + ("" if z["who"] == "all vehicles" else f" ({z['who']})"))
        out.append("")
    if b["routes"]:
        out.append("*Your routes*")
        for r in b["routes"]:
            if r.get("error"):
                out.append(f"• {r['name']} ({r['vehicle']}): no way through right now. Hold or call dispatch.")
                continue
            line = f"• {r['name']} ({r['vehicle']}): {r['minutes']} min, {r['km']} km"
            line += f", {r['delay_minutes']} min over usual" if r["affected"] else ", as usual"
            out.append(line)
            if r["affected"]:
                if r["via"]:
                    out.append(f"  Go via {', '.join(r['via'])}")
                out.append(f"  {r['maps_url']}")
        out.append("")
    out.append("Suggested routes, checked against today's advisories. Follow signs and police on the road.")
    return "\n".join(out)


def as_email_html(b: dict) -> str:
    """Email-safe: tables, inline styles, system fonts. Paste into any mail client or send as the HTML part."""
    e = escape
    font = "font-family:'Segoe UI',Arial,sans-serif;"
    zones = "".join(
        f"<tr><td style='padding:10px 12px;border:2px solid #1E1A05;background:#F2B705;color:#1E1A05;{font}'>"
        f"<b style='font-size:15px'>{e(z['cause'])}</b><br><span style='font-size:13px'>{e(z['where'])}</span><br>"
        f"<span style='font-size:12px'>{'Clearing, until' if z['clearing'] else 'Until'} about {e(z['until'])}"
        f"{'' if z['who'] == 'all vehicles' else ', ' + e(z['who'])}</span></td></tr><tr><td style='height:8px'></td></tr>"
        for z in b["zones"]) or f"<tr><td style='padding:10px 0;color:#5B636B;{font}font-size:14px'>No diversions on record right now.</td></tr>"
    routes = ""
    for r in b["routes"]:
        band = "#C8322B" if r.get("error") else "#B4581A" if r.get("affected") else "#1E7B4A"
        if r.get("error"):
            body = f"<b style='font-size:15px'>{e(r['name'])}</b> <span style='color:#5B636B'>{e(r['vehicle'])}</span><br>No way through right now. Hold or call dispatch."
        else:
            status = f"{r['delay_minutes']} min over its usual {r['usual_minutes']} min" if r["affected"] else "on its usual road"
            via = f"<br>Go via {e(', '.join(r['via']))}" if r["affected"] and r["via"] else ""
            link = f"<br><a href='{e(r['maps_url'])}' style='color:#1456A8;font-weight:600'>Open in Google Maps</a>" if r["affected"] else ""
            body = (f"<span style='color:#5B636B;font-size:13px'>{e(r['name'])}, {e(r['vehicle'])}</span><br>"
                    f"<b style='font-size:24px'>{r['minutes']} min</b> &nbsp;<b>{r['km']} km</b><br>"
                    f"<span style='font-size:13px'>{status}{via}</span>{link}")
        routes += (f"<tr><td style='border:1px solid #D5D9DC;border-left:8px solid {band};padding:10px 12px;background:#fff;color:#2B2F33;{font}font-size:14px'>{body}</td></tr>"
                   "<tr><td style='height:8px'></td></tr>")
    routes = routes or f"<tr><td style='padding:10px 0;color:#5B636B;{font}font-size:14px'>No team routes saved yet.</td></tr>"
    return (
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#EEF0F1'><tr><td align='center' style='padding:16px'>"
        "<table role='presentation' width='600' cellpadding='0' cellspacing='0' style='max-width:600px;width:100%'>"
        f"<tr><td style='background:#2B2F33;color:#fff;padding:16px 18px;{font}'><span style='font-size:13px;color:#C9CED2'>DetourAI</span><br>"
        f"<b style='font-size:22px'>{e(b['city'])} road brief</b><br><span style='font-size:13px;color:#C9CED2'>{e(b['date_label'])}, {e(b['time_label'])}</span></td></tr>"
        f"<tr><td style='background:#fff;padding:14px 18px;color:#2B2F33;{font}font-size:15px;line-height:1.45'>{e(b['summary'])}</td></tr>"
        f"<tr><td style='padding:18px 18px 4px;{font}font-size:13px;font-weight:600;color:#5B636B'>Avoid today</td></tr>"
        f"<tr><td style='padding:0 18px'><table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{zones}</table></td></tr>"
        f"<tr><td style='padding:14px 18px 4px;{font}font-size:13px;font-weight:600;color:#5B636B'>Your routes</td></tr>"
        f"<tr><td style='padding:0 18px'><table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{routes}</table></td></tr>"
        f"<tr><td style='padding:12px 18px 18px;{font}font-size:12px;color:#5B636B'>Suggested routes, checked against today's advisories. Follow signs and police on the road. "
        "Map data © OpenStreetMap contributors.</td></tr></table></td></tr></table>"
    )
