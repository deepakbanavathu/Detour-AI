from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL


def _configure(c) -> None:
    c.execute("SET pg_trgm.similarity_threshold = 0.45")   # the % operator threshold used by the gazetteer
    c.execute("SET statement_timeout = '60s'")              # one runaway search must not hold a connection for ever
    c.commit()                                              # the pool rejects a connection left inside a transaction


# sized for the server's worker threads: several vehicles are routed side by side, each on its own connection
pool = ConnectionPool(conninfo=DATABASE_URL, min_size=2, max_size=20, open=False,
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
