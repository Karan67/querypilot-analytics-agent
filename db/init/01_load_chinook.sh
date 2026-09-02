#!/bin/bash
#
# Postgres init hook 1 of 2 - load the sample dataset.
#
# Runs automatically, once, when the data volume is empty. Ordering matters:
# this file must run before 02_readonly_role.sh, because GRANT SELECT ON ALL
# TABLES only affects tables that already exist when the grant executes.
#
# The seed file is produced on the host by db/fetch_chinook.sh (or
# fetch_chinook.ps1) and bind-mounted at /seed.

set -euo pipefail

SEED_FILE="${CHINOOK_SEED_FILE:-/seed/chinook.sql}"

if [ ! -f "$SEED_FILE" ]; then
    echo "" >&2
    echo "ERROR: seed file not found at $SEED_FILE" >&2
    echo "" >&2
    echo "Fetch the dataset on the host first, then recreate the volume:" >&2
    echo "    ./db/fetch_chinook.sh          (Windows: .\\db\\fetch_chinook.ps1)" >&2
    echo "    docker compose down -v" >&2
    echo "    docker compose up --build" >&2
    echo "" >&2
    exit 1
fi

echo "[querypilot] Loading Chinook from $SEED_FILE into database '$POSTGRES_DB'..."

# ON_ERROR_STOP=1: a partially loaded dataset is worse than a failed startup,
# because the eval numbers it produces would be quietly wrong.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --quiet \
     --file "$SEED_FILE"

TABLE_COUNT=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")

echo "[querypilot] Chinook loaded. Tables in public schema: $TABLE_COUNT"

if [ "$TABLE_COUNT" -eq 0 ]; then
    echo "ERROR: the seed file ran but created no tables in public." >&2
    exit 1
fi
