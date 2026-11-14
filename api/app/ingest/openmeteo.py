"""Open-Meteo forecast API: hourly precipitation on a coarse grid. Free for non-commercial use; CC BY 4.0."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

URL = "https://api.open-meteo.com/v1/forecast"
API_K = 0.85            # antecedent precipitation index decay per day


@dataclass
class RainPoint:
    lat: float
    lng: float
    i_mm_h: float        # precipitation in the current hour
    acc_1h: float
    acc_3h: float
    api_72h: float


def grid(bbox: tuple[float, float, float, float], step: float) -> list[tuple[float, float]]:
    minx, miny, maxx, maxy = bbox
    pts, y = [], miny + step / 2
    while y < maxy:
        x = minx + step / 2
        while x < maxx:
            pts.append((round(y, 4), round(x, 4)))
            x += step
        y += step
    return pts


def aggregate(payload: list[dict] | dict, now: datetime | None = None) -> list[RainPoint]:
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    out: list[RainPoint] = []
    for loc in payload if isinstance(payload, list) else [payload]:
        h = loc.get("hourly") or {}
        times = [datetime.fromisoformat(t) for t in h.get("time", [])]
        rain = [float(v or 0.0) for v in h.get("precipitation", [])]
        if not times or now not in times:
            continue
        k = times.index(now)
        api = sum(rain[j] * API_K ** ((k - j) / 24.0) for j in range(max(0, k - 72), k))
        out.append(RainPoint(lat=float(loc["latitude"]), lng=float(loc["longitude"]), i_mm_h=rain[k],
                             acc_1h=rain[k - 1] if k >= 1 else 0.0, acc_3h=sum(rain[max(0, k - 3):k]), api_72h=round(api, 2)))
    return out


def poll(bbox: tuple[float, float, float, float], step: float = 0.05, timeout: float = 30.0) -> list[RainPoint]:
    pts = grid(bbox, step)
    out: list[RainPoint] = []
    for i in range(0, len(pts), 50):                 # 50 locations per request keeps URLs short
        chunk = pts[i:i + 50]
        r = httpx.get(URL, timeout=timeout, params={
            "latitude": ",".join(str(p[0]) for p in chunk), "longitude": ",".join(str(p[1]) for p in chunk),
            "hourly": "precipitation", "past_days": 3, "forecast_days": 1, "timezone": "GMT"})
        r.raise_for_status()
        out += aggregate(r.json())
    return out
