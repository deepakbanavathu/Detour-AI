# DetourAI v0 — demo stack on one machine. Targets run from the repo root.
SHELL := /bin/bash
DB_URL ?= postgresql://detour:$${DB_PASSWORD:-detour}@127.0.0.1:5432/detour
PBF ?= data/southern-zone-latest.osm.pbf

.PHONY: up down logs migrate import gazetteer turns smoke psql test

up:            ## build and start db, redpanda, api
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

migrate:       ## apply db/migrations/*.sql once each
	docker compose exec db bash /migrate.sh

import:        ## PBF=data/x.osm.pbf  (needs osmium + osm2pgsql on the host, or run inside a tools container)
	DATABASE_URL="$(DB_URL)" ./import/import_osm.sh "$(PBF)"

gazetteer:     ## rebuild geo.gazetteer from the loaded osm.* tables (import does this already)
	psql "$(DB_URL)" -v ON_ERROR_STOP=1 -f import/build_gazetteer.sql

turns:         ## optional: net.turn for GraphHopper (slow, millions of rows)
	psql "$(DB_URL)" -v ON_ERROR_STOP=1 -f import/build_turns.sql

smoke:         ## 2W vs SUV on a fixed pair of points
	./scripts/smoke.sh

psql:
	psql "$(DB_URL)"

test:          ## parser tests, no network or database (pip install -r api/requirements.txt pytest)
	python -m pytest -q tests
