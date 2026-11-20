"""Google Maps URL handoff (Delivery surfaces tab, 'Google Maps handoff').

Contract from Google's Maps URLs page: api=1 required, no key, travelmode in
{driving, two-wheeler, walking, bicycling, transit}, up to three waypoints on mobile,
URL under 2,048 characters. No dir_action=navigate when waypoints are present.
"""
from __future__ import annotations

from urllib.parse import urlencode

MAX_WAYPOINTS = 3
ARTERIAL_RANK = 3          # highway_rank <= 3 is where Maps already agrees with us
MIN_RUN_M = 300.0


def waypoints(edges: list[dict], max_wp: int = MAX_WAYPOINTS) -> list[tuple[float, float]]:
    """Fewest pins that make Maps reproduce the detour: midpoints of the longest off-arterial or zone-adjacent runs."""
    runs: list[list[tuple[int, dict]]] = []
    cur: list[tuple[int, dict]] = []
    for i, e in enumerate(edges):
        if e["highway_rank"] > ARTERIAL_RANK or e.get("zone_adjacent"):
            cur.append((i, e))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    def run_len(r: list[tuple[int, dict]]) -> float:
        return sum(x[1]["length_m"] for x in r)

    runs = [r for r in runs if run_len(r) >= MIN_RUN_M]
    keep = sorted(sorted(runs, key=run_len, reverse=True)[:max_wp], key=lambda r: r[0][0])

    pins: list[tuple[float, float]] = []
    for r in keep:
        half, acc = run_len(r) / 2.0, 0.0
        clean = [e for _, e in r if not e.get("zone_adjacent") and e["length_m"] > 0]
        if not clean:
            continue                      # a run that is all zone-adjacent gets no pin: never point Maps at a zone
        chosen = clean[-1]
        for e in clean:
            acc += e["length_m"]
            if acc >= half:
                chosen = e
                break
        pins.append((chosen["mid_lat"], chosen["mid_lng"]))
    return pins


def maps_url(profile_code: str, dest: tuple[float, float], pins: list[tuple[float, float]],
             origin: tuple[float, float] | None = None) -> str:
    q: dict[str, str] = {
        "api": "1",
        "destination": f"{dest[0]:.5f},{dest[1]:.5f}",
        "travelmode": "two-wheeler" if profile_code.upper() == "2W" else "driving",
        "utm_source": "detourai",
        "utm_campaign": "directions_request",
    }
    if origin is not None:
        q["origin"] = f"{origin[0]:.5f},{origin[1]:.5f}"
    if pins:
        q["waypoints"] = "|".join(f"{a:.5f},{b:.5f}" for a, b in pins)   # urlencode writes | as %7C and , as %2C
    url = "https://www.google.com/maps/dir/?" + urlencode(q)
    assert len(url) < 2048
    return url


def waze_url(dest: tuple[float, float]) -> str:
    """Opens Waze navigating to the destination. Waze takes one destination and no waypoints, so it chooses its own
    route: offer it as a second button, never as the carrier of DetourAI's detour."""
    return f"https://waze.com/ul?ll={dest[0]:.5f}%2C{dest[1]:.5f}&navigate=yes&utm_source=detourai"
