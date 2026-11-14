-- Extensions and schemas. h3_postgis provides h3_lat_lng_to_cell(geometry, int)
-- and h3_polygon_to_cells(geometry, int); plain h3 only takes PostgreSQL points.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS osm;        -- raw osm2pgsql flex output
CREATE SCHEMA IF NOT EXISTS net;        -- road network (Module 2)
CREATE SCHEMA IF NOT EXISTS geo;        -- gazetteer, zones (Module 1)
CREATE SCHEMA IF NOT EXISTS ingest;     -- raw messages, IR (Module 1)
CREATE SCHEMA IF NOT EXISTS flow;       -- rationing valve (Module 3)
CREATE SCHEMA IF NOT EXISTS hydro;      -- DEM, rain, sinks (Module 4)
CREATE SCHEMA IF NOT EXISTS telemetry;  -- fleet pings, edge windows (Modules 4, 5)
CREATE SCHEMA IF NOT EXISTS client;     -- subscribers, corridors, trigger grids (Module 5)
