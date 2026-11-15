from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL


def _configure(c) -> None:
    c.execute("SET pg_trgm.similarity_threshold = 0.45")   # the % operator threshold used by the gazetteer
    c.commit()                                              # the pool rejects a connection left inside a transaction


pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=8, open=False,
                      kwargs={"row_factory": dict_row}, configure=_configure)


def open_pool() -> None:
    pool.open()
    pool.wait()


def close_pool() -> None:
    pool.close()


@contextmanager
def conn():
    with pool.connection() as c:
        yield c
