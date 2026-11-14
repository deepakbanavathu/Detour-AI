#!/usr/bin/env bash
# Applies /migrations/*.sql in lexical order, once each. Runs inside the db container:
#   docker compose exec db bash /migrate.sh
set -euo pipefail
export PGUSER="${POSTGRES_USER:-detour}" PGDATABASE="${POSTGRES_DB:-detour}" PGPASSWORD="${POSTGRES_PASSWORD:-detour}"

psql -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.migration (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

for f in /migrations/*.sql; do
  name=$(basename "$f")
  if psql -tAq -c "SELECT 1 FROM meta.migration WHERE filename = '$name'" | grep -q 1; then
    echo "skip   $name"
    continue
  fi
  echo "apply  $name"
  psql -v ON_ERROR_STOP=1 -q -f "$f"
  psql -v ON_ERROR_STOP=1 -q -c "INSERT INTO meta.migration (filename) VALUES ('$name')"
done
echo "migrations complete"
