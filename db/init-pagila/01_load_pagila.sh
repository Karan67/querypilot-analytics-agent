#!/bin/bash
#
# Postgres init hook 1 of 2 - load the Pagila sample dataset.
#
# Iteration 14 (specs/017-schema-generality.md). Runs automatically, once,
# when pagila-db's data volume is empty. Ordering matters, same reason as
# db/init/01_load_chinook.sh: this must run before 02_readonly_role.sh, since
# GRANT SELECT ON ALL TABLES only affects tables that already exist when the
# grant executes.
#
# The seed files are produced on the host by db/fetch_pagila.sh (or
# fetch_pagila.ps1) and bind-mounted at /seed. Two files, not one: Pagila
# ships schema and data separately, unlike Chinook's single dump.
#
# pagila-db's superuser is deliberately named "postgres" (see
# docker-compose.yml's pagila-db service), not "querypilot" as Chinook's is.
# Pagila's dump carries ~106 `OWNER TO postgres` / `ALTER ... OWNER TO
# postgres` statements; matching the role name avoids rewriting every one, the
# way db/fetch_chinook.sh has to strip a DROP/CREATE DATABASE preamble Pagila
# does not have.

set -euo pipefail

SCHEMA_FILE="${PAGILA_SCHEMA_FILE:-/seed/pagila-schema.sql}"
DATA_FILE="${PAGILA_DATA_FILE:-/seed/pagila-data.sql}"

for f in "$SCHEMA_FILE" "$DATA_FILE"; do
    if [ ! -f "$f" ]; then
        echo "" >&2
        echo "ERROR: seed file not found at $f" >&2
        echo "" >&2
        echo "Fetch the dataset on the host first, then recreate the volume:" >&2
        echo "    ./db/fetch_pagila.sh          (Windows: .\\db\\fetch_pagila.ps1)" >&2
        echo "    docker compose --profile pagila down -v" >&2
        echo "    docker compose --profile pagila up -d pagila-db" >&2
        echo "" >&2
        exit 1
    fi
done

echo "[querypilot] Loading Pagila schema from $SCHEMA_FILE into database '$POSTGRES_DB'..."

# ON_ERROR_STOP=1: a partially loaded dataset is worse than a failed startup,
# same reasoning as Chinook's loader.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --quiet \
     --file "$SCHEMA_FILE"

echo "[querypilot] Loading Pagila data from $DATA_FILE into database '$POSTGRES_DB'..."

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --quiet \
     --file "$DATA_FILE"

TABLE_COUNT=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';")

echo "[querypilot] Pagila loaded. Base tables in public schema: $TABLE_COUNT"

if [ "$TABLE_COUNT" -eq 0 ]; then
    echo "ERROR: the seed files ran but created no tables in public." >&2
    exit 1
fi
