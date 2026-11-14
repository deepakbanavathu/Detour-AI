"""Vehicle profiles from net.vehicle_profile, cached at startup."""
from dataclasses import dataclass

from .db import conn


@dataclass(frozen=True)
class Profile:
    profile_id: int
    code: str
    width_m: float
    height_m: float
    ground_clearance_m: float
    can_filter: bool
    max_speed_kmh: float

    @property
    def bit(self) -> int:
        return 1 << self.profile_id


_profiles: dict[str, Profile] = {}


def load() -> None:
    with conn() as c:
        rows = c.execute(
            "SELECT profile_id, code, width_m, height_m, ground_clearance_m, can_filter, max_speed_kmh "
            "FROM net.vehicle_profile ORDER BY profile_id"
        ).fetchall()
    _profiles.clear()
    for r in rows:
        _profiles[r["code"]] = Profile(**r)


def get(code: str) -> Profile:
    if not _profiles:
        load()
    try:
        return _profiles[code.upper()]
    except KeyError:
        raise ValueError(f"unknown profile {code!r}; known: {', '.join(_profiles)}") from None


def codes() -> list[str]:
    if not _profiles:
        load()
    return list(_profiles)
