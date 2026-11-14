"""What an alert says, on each channel, and when a subscriber may receive one. Pure functions."""
from __future__ import annotations

CAUSE = {"WATERLOGGING": "Waterlogging", "VIP": "VIP movement", "ROADWORK": "Road work", "ACCIDENT": "Accident",
         "PROCESSION": "Procession", "TREE_FALL": "Fallen tree", "GATE": "Road closed", "UNKNOWN": "Restriction"}
LEAD_MIN = 30


def in_window(windows: list[tuple[int, int]], minute_of_day: int, lead: int = LEAD_MIN) -> bool:
    """True inside a travel window or up to `lead` minutes before it starts."""
    return any(lo - lead <= minute_of_day < hi for lo, hi in windows)


def alert_text(cause: str, where: str, until: str, via: list[str], trip: str, extra_min: int | None) -> str:
    """Free-form alert (inside WhatsApp's 24 h window, and on Telegram)."""
    lines = [f"{CAUSE.get(cause, 'Restriction')}: {where}.", f"It touches your {trip} trip, until about {until}."]
    if via:
        lines.append("Suggested way round: via " + ", ".join(via[:3]) + (f" (about {extra_min} min longer)." if extra_min else "."))
    lines.append("Suggested only: follow signs and police on the road.")
    return "\n".join(lines)[:1000]


def template_params(cause: str, where: str, until: str, via: list[str], trip: str) -> list[str]:
    """Body variables for the approved utility template, in order: cause, where, until, via, trip."""
    clean = lambda s: " ".join(str(s).split())[:120] or "-"          # noqa: E731  (template variables allow no newlines)
    return [clean(CAUSE.get(cause, "Restriction")), clean(where), clean(until), clean(", ".join(via[:3]) or "local roads"), clean(trip)]


def cleared_text(cause: str, where: str) -> str:
    return f"Cleared: {CAUSE.get(cause, 'Restriction').lower()} {where}. Your usual route is open again."


def sms_text(cause: str, where: str, via: list[str], link: str | None) -> str:
    """A person with no data must be able to act on this alone. Kept inside one or two SMS segments."""
    base = f"DetourAI: {CAUSE.get(cause, 'Restriction')} {where}. Go via {', '.join(via[:2]) or 'another road'}."
    if link and len(base) + len(link) + 1 <= 300:
        base += " " + link
    return base[:300]


def trip_label(label: str) -> str:
    return {"home-work": "morning", "work-home": "evening"}.get(label, "regular")
