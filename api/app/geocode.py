"""Fallback place search for names the gazetteer does not hold: an OpenStreetMap geocoder (Photon by default).

Used only when the city's own gazetteer has fewer than three matches. Answers are cached, the call is limited to the
city's bounding box, and any failure simply means no extra suggestions. GEOCODER_URL='' switches it off.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

import httpx

GEOCODER_URL = os.environ.get("GEOCODER_URL", "https://photon.komoot.io/api/")
_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_LOCK = threading.Lock()
KIND = {"suburb": "area", "neighbourhood": "area", "quarter": "area", "locality": "area", "village": "area", "town": "area", "city": "area",
        "residential": "area", "primary": "road", "secondary": "road", "tertiary": "road", "trunk": "road"}


def parse(payload: dict, bbox: tuple[float, float, float, float]) -> list[dict]:
    """Photon's GeoJSON answer as suggestions, keeping only named points inside the city."""
    out, seen = [], set()
    minx, miny, maxx, maxy = bbox
    for f in payload.get("features", []):
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        name = (p.get("name") or "").strip()
        if not name or g.get("type") != "Point":
            continue
        lng, lat = g["coordinates"][:2]
        if not (minx <= lng <= maxx and miny <= lat <= maxy) or name.lower() in seen:
            continue
        seen.add(name.lower())
        near = p.get("district") or p.get("locality") or p.get("city")
        out.append({"gaz_id": None, "name": name, "kind": KIND.get(p.get("osm_value"), p.get("osm_value") or "place"),
                    "lat": float(lat), "lng": float(lng), "source": "osm-search", "near": near if near and near != name else None})
    return out


def search(q: str, bbox: tuple[float, float, float, float], limit: int = 6, timeout: float = 2.5) -> list[dict]:
    if not GEOCODER_URL or len(q) < 4:
        return []
    key = (q.lower(), bbox)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    try:
        r = httpx.get(GEOCODER_URL, timeout=timeout, headers={"User-Agent": "DetourAI-console"},
                      params={"q": q, "limit": limit, "lang": "en", "bbox": ",".join(f"{v:.4f}" for v in bbox),
                              "lat": (bbox[1] + bbox[3]) / 2, "lon": (bbox[0] + bbox[2]) / 2})
        res = parse(r.json(), bbox) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []                                  # not cached: the next keystroke may succeed
    with _LOCK:
        _CACHE[key] = res
        while len(_CACHE) > 500:
            _CACHE.popitem(last=False)
    return res
