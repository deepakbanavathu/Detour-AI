"""Fixture for tests that drive the application code against a real database (pytest -m db)."""
import os

import pytest

URL = os.environ.get("DETOUR_DB")


@pytest.fixture(scope="module")
def db():
    if not URL:
        pytest.skip("DETOUR_DB not set")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    import app.db as d
    from app import profiles
    d.pool = ConnectionPool(conninfo=URL, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row}, configure=d._configure)
    profiles.load()
    with d.conn() as c:
        if not c.execute("SELECT count(*) AS n FROM net.edge").fetchone()["n"]:
            pytest.skip("no road network imported")
        yield c
    d.pool.close()


def onboard(c, user: str, home: dict, work: dict, vehicle: str = "veh_2w") -> int:
    """A throwaway Telegram subscriber with a morning and an evening trip; returns the subscriber id."""
    from app.channels import store
    for m in ({"kind": "text", "text": "hi"}, {"kind": "button", "id": "consent_yes"}, {"kind": "button", "id": vehicle},
              {"kind": "location", "lat": home["lat"], "lng": home["lng"]}, {"kind": "location", "lat": work["lat"], "lng": work["lng"]},
              {"kind": "button", "id": "win_both"}):
        store.handle_inbound(c, "TELEGRAM", user, m)
    return store._sub_id(c, "TELEGRAM", store.ident("TELEGRAM", user))


def forget(c, users: list[str], zones: list[int]) -> None:
    from app.channels import store
    c.rollback()
    for u in users:
        idh = store.ident("TELEGRAM", u)
        c.execute("DELETE FROM client.subscriber WHERE channel = 'TELEGRAM' AND channel_id_hmac = %s", (idh,))
        c.execute("DELETE FROM client.chat_state WHERE channel = 'TELEGRAM' AND channel_id_hmac = %s", (idh,))
    c.execute("DELETE FROM client.report WHERE zone_id = ANY(%s)", (zones,))
    c.execute("UPDATE geo.zone_of_avoidance SET status = 'EXPIRED' WHERE zone_id = ANY(%s) AND status <> 'REFUTED'", (zones,))
    c.execute("SELECT geo.reapply_all_zones()")
    c.commit()


def long_road(c) -> tuple[str, list[dict]]:
    """The longest named arterial and the midpoints of its segments, end to end."""
    road = c.execute("SELECT name FROM net.edge WHERE name IS NOT NULL AND highway_rank <= 4 GROUP BY name ORDER BY sum(length_m) DESC LIMIT 1").fetchone()["name"]
    pts = c.execute("""SELECT ST_Y(ST_LineInterpolatePoint(geom, 0.5)) lat, ST_X(ST_LineInterpolatePoint(geom, 0.5)) lng FROM net.edge
                       WHERE name = %s ORDER BY ST_X(ST_StartPoint(geom)) + ST_Y(ST_StartPoint(geom))""", (road,)).fetchall()
    return road, pts
