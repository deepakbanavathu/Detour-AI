#!/usr/bin/env python3
"""Inputs for the synthetic network (make_synthetic_osm.py), with faults planted on purpose.

Used to test the verifier itself: verify/fixtures/expected.json lists which criteria must fail on these inputs
(the planted faults) and which must pass. If the verifier stops catching a planted fault, CI goes red.
Usage: make_synthetic_inputs.py OUT_DIR
"""
import json
import math
import random
import sys
from pathlib import Path

LAT0, LNG0, DLAT, DLNG = 17.4100, 78.4300, 0.0018, 0.0019


def g(r, c):
    return LAT0 + r * DLAT, LNG0 + c * DLNG


def track(waypoints, step_m=10.0, noise_m=4.0, seed=1):
    rnd, pts = random.Random(seed), []
    for (a, b), (c, d) in zip(waypoints, waypoints[1:], strict=False):
        dist = math.hypot((d - b) * 111320 * math.cos(math.radians(a)), (c - a) * 110540)
        n = max(1, int(dist / step_m))
        for i in range(n):
            t = i / n
            pts.append((a + (c - a) * t + rnd.gauss(0, noise_m) / 110540, b + (d - b) * t + rnd.gauss(0, noise_m) / 106000))
    pts.append(waypoints[-1])
    return pts


def gpx(pts):
    body = "".join(f'<trkpt lat="{la:.7f}" lon="{lo:.7f}"><time>2026-11-15T08:{i // 60 % 60:02d}:{i % 60:02d}Z</time></trkpt>' for i, (la, lo) in enumerate(pts))
    return f'<?xml version="1.0"?><gpx version="1.1" creator="synthetic" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{body}</trkseg></trk></gpx>'


def main(out: Path):
    (out / "gpx").mkdir(parents=True, exist_ok=True)
    rows = [
        ("s01", "Road No 1 west", g(0, 0), "Road No 1 east", g(0, 5), "both", "2", "Road No 1 Banjara Hills", "s01_car.gpx"),
        ("s02", "SW corner", g(0, 0), "NE corner", g(5, 5), "car", "5", "Road No 36", ""),
        ("s03", "Gate west", g(1, 1), "Gate east", g(2, 2), "both", "1.5", "", "s03_2w.gpx"),
        ("s04", "Spur start", g(0, 4), "Spur end", (LAT0 - DLAT, LNG0 + 5 * DLNG), "both", "1", "Expressway Spur", ""),
    ]
    with (out / "pairs.csv").open("w") as f:
        f.write("id,from_label,from_lat,from_lng,to_label,to_lat,to_lng,vehicle,usual_minutes,usual_via,gpx\n")
        for i, fl, a, tl, b, v, m, via, gp in rows:
            f.write(f"{i},{fl},{a[0]:.6f},{a[1]:.6f},{tl},{b[0]:.6f},{b[1]:.6f},{v},{m},{via},{gp}\n")
    gate = (LAT0 + 1.5 * DLAT, LNG0 + 1.5 * DLNG)
    with (out / "gates.csv").open("w") as f:
        f.write("id,lat,lng,width_m,scooter_passes,car_passes,a_lat,a_lng,b_lat,b_lng,notes\n")
        f.write(f"g01,{gate[0]:.6f},{gate[1]:.6f},1.5,yes,no,{g(1, 1)[0]:.6f},{g(1, 1)[1]:.6f},{g(2, 2)[0]:.6f},{g(2, 2)[1]:.6f},planted: 1.5 m two-way lane\n")
    # car along Road No 1 eastward (clean); scooter through the gate lane (planted: mask forbids it);
    # car westward along 'Oneway East' (planted: driving against a mapped one-way)
    (out / "gpx" / "s01_car.gpx").write_text(gpx(track([g(0, 0), g(0, 5)], seed=1)))
    (out / "gpx" / "s03_2w.gpx").write_text(gpx(track([g(1, 0), g(1, 1), gate, g(2, 2), g(2, 3)], seed=2)))
    (out / "gpx" / "t09_car.gpx").write_text(gpx(track([g(2, 5), g(2, 1)], seed=3)))

    def line(*pts):
        return {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in pts]}

    feats = [
        ("a1", "Waterlogging at Tolichowki Underpass. Commuters are advised to take alternate routes", "zone", "Tolichowki Underpass", line(g(4, 2), g(4, 3))),
        ("a2", "Traffic restricted between Jubilee Hills Check Post and Madhapur due to VIP movement from 10 am to 12 pm", "zone",
         "Jubilee Hills Check Post; Madhapur", line(g(0, 3), g(0, 0), g(5, 0))),
        ("a3", "Traffic diverted from Jubilee Hills Check Post towards Madhapur via Road No 36 due to waterlogging", "zone", "Jubilee Hills Check Post; Madhapur",
         line(g(0, 3), g(0, 2), g(0, 1), g(0, 0))),
        ("a4", "Waterlogging at Zzyzx Nagar underpass, vehicles slow moving", "zone", "", line(g(3, 3), g(3, 4))),
        ("n1", "Traffic police conducted a special drive against wrong side driving in the city", "none", "", None),
        ("n2", "Traffic restored and normal at Tolichowki Underpass after water receded", "lifted", "", None),
        ("n3", "Gold prices dip in Hyderabad markets today", "none", "", None),
    ]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": i, "text": t, "source": "@fixture", "posted_at": "2026-11-15T08:00:00+05:30", "expect": e, "anchors": an},
         "geometry": geom} for i, t, e, an, geom in feats]}
    (out / "advisories.geojson").write_text(json.dumps(fc, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
