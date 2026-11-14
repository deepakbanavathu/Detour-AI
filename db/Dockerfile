# PostGIS 16 with pgRouting, h3-pg (incl. h3_postgis) and TimescaleDB.
# The postgis/postgis image already carries the PGDG apt repository, which is
# where postgresql-16-pgrouting and postgresql-16-h3 come from.
FROM postgis/postgis:16-3.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg lsb-release \
 && curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/timescale.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/timescale.gpg] https://packagecloud.io/timescale/timescaledb/debian/ $(lsb_release -cs) main" \
      > /etc/apt/sources.list.d/timescaledb.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16-pgrouting \
      postgresql-16-h3 \
      timescaledb-2-postgresql-16 \
      timescaledb-toolkit-postgresql-16 \
 && rm -rf /var/lib/apt/lists/*

# If postgresql-16-h3 is ever missing from PGDG for this Debian release, build
# h3-pg from source instead:  pgxn install h3   (needs postgresql-server-dev-16, cmake)
