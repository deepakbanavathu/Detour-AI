# DetourAI v0 — demo stack on one machine. Targets run from the repo root.
SHELL := /bin/bash
DB_URL ?= postgresql://detour:$${DB_PASSWORD:-detour}@127.0.0.1:5432/detour
PBF ?= data/southern-zone-latest.osm.pbf

.PHONY: up down logs migrate import gazetteer turns smoke psql test test-sql test-ui test-live test-all

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

test:          ## unit tests: parsers, normalization, maps, brief, packs, routing SQL (no network, no database)
	python -m pytest tests --ignore=tests/ui --ignore=tests/integration

test-sql:      ## parse every SQL file and embedded statement with PostgreSQL's parser
	python scripts/check_sql.py

test-ui:       ## browser tests on preview data (pip install -r requirements-dev.txt; python -m playwright install chromium)
	python -m pytest tests/ui

test-live:     ## end-to-end against the running stack (needs make up, migrate, import)
	python -m pytest -m live tests/integration

test-all: test test-sql test-ui test-live
