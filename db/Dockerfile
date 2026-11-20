# PostgreSQL 16 with PostGIS, pgRouting, h3-pg and TimescaleDB, on Debian bookworm.
#
# Built on the official postgres image, not postgis/postgis:16-3.4: that image sits on Debian bullseye, whose
# PostgreSQL package repository has been retired, so nothing could be installed on top of it any more.
FROM postgres:16-bookworm

# 1. PostGIS (with raster, which h3_postgis needs), pgRouting and h3 from the PostgreSQL project's repository,
#    which this base image already has configured.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16-postgis-3 \
      postgresql-16-postgis-3-scripts \
      postgresql-16-pgrouting \
      postgresql-16-h3 \
 && rm -rf /var/lib/apt/lists/*

# 2. TimescaleDB from Timescale's own repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey | gpg --dearmor -o /usr/share/keyrings/timescale.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/timescale.gpg] https://packagecloud.io/timescale/timescaledb/debian/ bookworm main" \
      > /etc/apt/sources.list.d/timescaledb.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends timescaledb-2-postgresql-16 \
 && rm -rf /var/lib/apt/lists/*

# If a step fails, the message names the package. postgresql-16-h3 missing: build h3-pg from source with
#   apt-get install -y postgresql-server-dev-16 cmake make gcc g++ pgxnclient && pgxn install h3
