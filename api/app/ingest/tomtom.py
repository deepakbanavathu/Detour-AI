"""TomTom Traffic Incidents (incident details by bounding box): closures and hazards with geometry attached."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"
FIELDS = ("{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,"
          "events{description,code},startTime,endTime,from,to,length,delay,roadNumbers}}}")
# iconCategory -> (event_type, cause, penalty). Jams (6) are not zones; they feed speeds in a later slice.
CATEGORY = {
    1: ("ACCIDENT", "ACCIDENT", 0.7),
    3: ("CLOSURE", "UNKNOWN", 0.4),          # dangerous conditions
    7: ("CLOSURE", "ROADWORK", 0.4),         # lane closed
    8: ("CLOSURE", "UNKNOWN", 1.0),          # road closed
    9: ("CLOSURE", "ROADWORK", 0.6),
    11: ("WATERLOGGING", "WATERLOGGING", 1.0),
    14: ("ACCIDENT", "ACCIDENT", 0.5),       # broken-down vehicle
}


@dataclass
class Incident:
    ext_id: str
    event_type: str
    cause: str
    penalty: float
    geometry: dict            # GeoJSON LineString or Point
    description: str
    frm: str | None
    to: str | None
    start: datetime
    end: datetime | None


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_incidents(payload: dict, now: datetime | None = None) -> list[Incident]:
    now = now or datetime.now(timezone.utc)
    out: list[Incident] = []
    for inc in payload.get("incidents", []):
        p, g = inc.get("properties") or {}, inc.get("geometry") or {}
        cat = p.get("iconCategory")
        if cat not in CATEGORY or g.get("type") not in ("LineString", "Point") or not g.get("coordinates"):
            continue
        event_type, cause, penalty = CATEGORY[cat]
        desc = "; ".join(e.get("description", "") for e in p.get("events") or [] if e.get("description"))
        out.append(Incident(
            ext_id=str(p.get("id")), event_type=event_type, cause=cause, penalty=penalty,
            geometry={"type": g["type"], "coordinates": g["coordinates"]},
            description=desc or event_type.title(), frm=p.get("from"), to=p.get("to"),
            start=_ts(p.get("startTime")) or now, end=_ts(p.get("endTime")),
        ))
    return out


def poll(key: str, bbox: tuple[float, float, float, float], categories: list[int], timeout: float = 20.0) -> list[Incident]:
    params = {"key": key, "bbox": ",".join(f"{v:.5f}" for v in bbox), "fields": FIELDS, "language": "en-GB",
              "categoryFilter": ",".join(str(c) for c in categories), "timeValidityFilter": "present"}
    r = httpx.get(URL, params=params, timeout=timeout)
    r.raise_for_status()
    return parse_incidents(r.json())


def valid_to(i: Incident, now: datetime, interval_s: int) -> datetime:
    """Hold a zone for two poll intervals past 'now' unless TomTom gives an end time that is later."""
    hold = now + timedelta(seconds=2 * interval_s)
    return max(hold, i.end) if i.end else hold
