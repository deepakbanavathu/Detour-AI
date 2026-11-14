#!/usr/bin/env bash
# Usage (on the host, with osmium + osm2pgsql installed, or inside a tools container):
#   DATABASE_URL=postgresql://detour:detour@127.0.0.1:5432/detour ./import/import_osm.sh data/southern-zone-latest.osm.pbf
#
# Get the source file from Geofabrik: https://download.geofabrik.de/asia/india/southern-zone.html
# BBOX is min_lon,min_lat,max_lon,max_lat. Default covers the GHMC area plus a margin.
set -euo pipefail

PBF=${1:?path to a .osm.pbf}
BBOX=${BBOX:-78.20,17.20,78.75,17.65}
DATABASE_URL=${DATABASE_URL:?set DATABASE_URL}
WORK=${WORK:-/tmp/detourai}
mkdir -p "$WORK"

echo "== 1/5 extract bbox $BBOX"
osmium extract --bbox "$BBOX" --strategy complete_ways "$PBF" -o "$WORK/city.osm.pbf" --overwrite

echo "== 2/5 keep roads, point features, restrictions"
osmium tags-filter "$WORK/city.osm.pbf" \
    w/highway n/barrier n/highway=traffic_signals n/traffic_calming n/highway=crossing r/type=restriction \
    n/name n/place w/place w/landuse w/boundary r/place r/landuse r/boundary=administrative \
    -o "$WORK/roads.osm.pbf" --overwrite

echo "== 3/5 osm2pgsql flex load into schema osm"
osm2pgsql --slim --drop --output=flex --style="$(dirname "$0")/detour.lua" \
    --cache 2000 -d "$DATABASE_URL" "$WORK/roads.osm.pbf"

echo "== 4/5 build net.* from osm.*"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$(dirname "$0")/build_network.sql"

echo "== 5/5 build the gazetteer from osm.*"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$(dirname "$0")/build_gazetteer.sql"

echo "== done"
psql "$DATABASE_URL" -tAc "SELECT 'nodes '||count(*) FROM net.node UNION ALL SELECT 'edges '||count(*) FROM net.edge"
