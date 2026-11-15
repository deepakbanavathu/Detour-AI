"""Text normalization (blueprint 1.3) and gazetteer resolution with disambiguation (1.5).

Deterministic by design: the same text and the same gazetteer give the same anchor.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import CITY_ID
from .db import conn

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_MENTION = re.compile(r"(?<!\w)@\w+")
_HASH = re.compile(r"#(\w+)")
_NON_TEXT = re.compile(r"[^a-z0-9/\-\s.,;:()]+")
_WS = re.compile(r"\s+")

RESOLVE_MIN_SCORE = 0.62
RESOLVE_MIN_LEAD = 0.08

_abbrev: dict[int, list[tuple[re.Pattern, str]]] = {}


def _load_abbrev() -> None:
    with conn() as c:
        rows = c.execute("SELECT city_id, short, long FROM geo.abbreviation ORDER BY length(short) DESC").fetchall()
    _abbrev.clear()
    for r in rows:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(r["short"]) + r"(?![a-z0-9])")
        _abbrev.setdefault(int(r["city_id"]), []).append((pat, r["long"]))


def set_abbreviations(rows: list[tuple[int, str, str]]) -> None:
    """Load abbreviations from memory instead of the database: (city_id, short, long). Used by tests."""
    _abbrev.clear()
    for city_id, short, long in sorted(rows, key=lambda r: -len(r[1])):
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(short) + r"(?![a-z0-9])")
        _abbrev.setdefault(int(city_id), []).append((pat, long))


def normalize(text: str, city_id: int = CITY_ID) -> str:
    """NFC, strip URLs/mentions/emoji, lowercase, expand abbreviations, collapse whitespace."""
    if not _abbrev:
        _load_abbrev()
    t = unicodedata.normalize("NFC", text)
    t = _URL.sub(" ", t)
    t = _MENTION.sub(" ", t)
    t = _HASH.sub(r"\1", t)
    t = t.lower()
    t = _NON_TEXT.sub(" ", t)
    for cid in (0, city_id):
        for pat, long in _abbrev.get(cid, []):
            t = pat.sub(long, t)
    return _WS.sub(" ", t).strip()


@dataclass
class Anchor:
    text: str
    gaz_id: int
    canonical: str
    feature_class: str
    geom_wkt: str          # EWKT in 4326
    score: float
    runner_up: float


class Unresolved(Exception):
    def __init__(self, text: str, candidates: list[dict]):
        super().__init__(f"could not resolve {text!r}")
        self.text = text
        self.candidates = candidates


_RESOLVE_SQL = """
SELECT g.gaz_id, g.canonical, g.feature_class, ST_AsEWKT(g.geom) AS geom_wkt,
       ST_Y(ST_Centroid(g.geom)) AS clat, ST_X(ST_Centroid(g.geom)) AS clng,
       0.55 * similarity(a.alias, %(q)s::text) * a.weight
     -- no reference point: neutral 0.5. (LEAST ignores NULLs, so COALESCE around it would score 0, not 0.5.)
     + 0.25 * CASE WHEN %(near)s::text IS NULL THEN 0.5
                   ELSE 1 - LEAST(ST_Distance(g.geom::geography, ST_GeomFromEWKT(%(near)s::text)::geography), 15000) / 15000 END
     + 0.10 * LEAST(g.popularity / 8, 1)
     + 0.10 * (g.beat_zone IS NOT DISTINCT FROM %(beat)s::text AND %(beat)s::text IS NOT NULL)::int AS score
FROM geo.gazetteer_alias a JOIN geo.gazetteer g USING (gaz_id)
WHERE g.city_id = %(city)s::smallint AND a.alias %% %(q)s::text
  AND (%(within)s::text IS NULL OR ST_DWithin(g.geom::geography, ST_GeomFromEWKT(%(within)s::text)::geography, 1200))
ORDER BY score DESC
LIMIT 8
"""

# More specific classes win when one place is stored twice (a named underpass is also a named road).
_CLASS_RANK = {"UNDERPASS": 0, "FLYOVER": 0, "BRIDGE": 0, "JUNCTION": 1, "SIGNAL": 1, "GATE": 1, "LANDMARK": 2, "AREA": 3, "ROAD": 4}
SAME_PLACE_M = 300.0


def _collapse(rows: list[dict]) -> list[dict]:
    """Merge candidates that are the same name at the same place, keeping the best score and the most specific class."""
    import math
    out: list[dict] = []
    for r in rows:
        twin = next((o for o in out if o["canonical"].lower() == r["canonical"].lower() and
                     math.hypot((o["clat"] - r["clat"]) * 110540, (o["clng"] - r["clng"]) * 111320 * math.cos(math.radians(r["clat"]))) <= SAME_PLACE_M), None)
        if twin is None:
            out.append(dict(r))
        elif _CLASS_RANK.get(r["feature_class"], 5) < _CLASS_RANK.get(twin["feature_class"], 5):
            twin.update({k: r[k] for k in ("gaz_id", "feature_class", "geom_wkt", "clat", "clng")})
            twin["score"] = max(float(twin["score"]), float(r["score"]))
    return out


def resolve(c, text: str, city_id: int = CITY_ID, near_wkt: str | None = None,
            beat: str | None = None, within_wkt: str | None = None) -> Anchor:
    """Resolve an anchor string to one gazetteer feature or raise Unresolved with the candidates."""
    q = normalize(text, city_id)
    rows = _collapse(c.execute(_RESOLVE_SQL, {"q": q, "near": near_wkt, "beat": beat, "city": city_id, "within": within_wkt}).fetchall())
    if not rows:
        raise Unresolved(text, [])
    top = rows[0]
    runner = float(rows[1]["score"]) if len(rows) > 1 else 0.0
    if float(top["score"]) < RESOLVE_MIN_SCORE or float(top["score"]) - runner < RESOLVE_MIN_LEAD:
        raise Unresolved(text, [dict(r) for r in rows[:3]])
    return Anchor(text=text, gaz_id=int(top["gaz_id"]), canonical=top["canonical"], feature_class=top["feature_class"],
                  geom_wkt=top["geom_wkt"], score=float(top["score"]), runner_up=runner)


def candidates(c, text: str, city_id: int = CITY_ID, limit: int = 5) -> list[dict]:
    q = normalize(text, city_id)
    rows = _collapse(c.execute(_RESOLVE_SQL, {"q": q, "near": None, "beat": None, "city": city_id, "within": None}).fetchall())
    return rows[:limit]
