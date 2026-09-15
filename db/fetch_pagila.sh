#!/usr/bin/env bash
#
# Fetch the Pagila PostgreSQL dump into db/pagila-seed/.
#
# Iteration 14 (specs/017-schema-generality.md). Pagila is the second, non-
# Chinook schema used to prove the engine reads a live catalog rather than
# Chinook's own. Run this on the HOST, once, before `docker compose --profile
# pagila up -d pagila-db`. The dump is not committed to the repo;
# db/init-pagila/01_load_pagila.sh loads whatever this script leaves behind.
#
#   ./db/fetch_pagila.sh            # no-op if the seed already exists
#   ./db/fetch_pagila.sh --force    # re-download
#
# Override the source with PAGILA_SCHEMA_URL / PAGILA_DATA_URL if the upstream
# path ever moves.
#
# Unlike Chinook's single-file dump, Pagila ships schema and data separately
# and neither needs the DROP/CREATE DATABASE stripping Chinook's does --
# confirmed by reading both files (2026-09-16): no database-management
# statements, no `\c`. What they do carry is ~106 `OWNER TO postgres`
# statements, which is why db/init-pagila/01_load_pagila.sh loads them under a
# superuser actually named `postgres` rather than rewriting every one.
#
# One block IS stripped: upstream's `film_embedding` table, its `vector`
# extension and its HNSW index. Measured directly (2026-09-16): loading it
# needs the pgvector extension, which no stock `postgres` image ships, and
# pulling a pgvector-flavoured image was judged out of proportion to one bonus
# column when this iteration's target types (tsvector, arrays, domains, uuid)
# are already present elsewhere in the same schema. Stripped by dropping any
# pg_dump paragraph naming `film_embedding`, the `vector` extension, or its
# operator classes -- paragraph-mode awk, the same shape as Chinook's
# preamble strip below, just on a different pattern.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_DIR="${SCRIPT_DIR}/pagila-seed"
SCHEMA_FILE="${SEED_DIR}/pagila-schema.sql"
DATA_FILE="${SEED_DIR}/pagila-data.sql"

PAGILA_SCHEMA_URL="${PAGILA_SCHEMA_URL:-https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-schema.sql}"
PAGILA_DATA_URL="${PAGILA_DATA_URL:-https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-data.sql}"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ -f "$SCHEMA_FILE" ] && [ -f "$DATA_FILE" ] && [ "$FORCE" -eq 0 ]; then
    echo "Seed already present: $SCHEMA_FILE, $DATA_FILE"
    echo "Re-download with: $0 --force"
    exit 0
fi

mkdir -p "$SEED_DIR"

fetch() {
    local url="$1" out="$2"
    echo "Downloading: $url"
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --show-error --silent -o "$out" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet -O "$out" "$url"
    else
        echo "ERROR: neither curl nor wget is available." >&2
        exit 1
    fi
}

RAW_SCHEMA="${SEED_DIR}/.pagila-schema.raw.sql"
RAW_DATA="${SEED_DIR}/.pagila-data.raw.sql"

fetch "$PAGILA_SCHEMA_URL" "$RAW_SCHEMA"
fetch "$PAGILA_DATA_URL" "$RAW_DATA"

# See the header comment: drops the film_embedding table (and the `vector`
# extension it alone needs) from both files, paragraph by pg_dump paragraph.
awk -v RS="" -v ORS="\n\n" \
    '!/film_embedding/ && !/[Ee][Xx][Tt][Ee][Nn][Ss][Ii][Oo][Nn].*vector/ && !/public\.vector\(/ && !/vector_cosine_ops/' \
    "$RAW_SCHEMA" > "$SCHEMA_FILE"
awk -v RS="" -v ORS="\n\n" '!/film_embedding/' "$RAW_DATA" > "$DATA_FILE"

rm -f "$RAW_SCHEMA" "$RAW_DATA"

SCHEMA_TABLES=$(grep -ic '^[[:space:]]*create[[:space:]]\+table' "$SCHEMA_FILE" || true)
SCHEMA_BYTES=$(wc -c < "$SCHEMA_FILE" | tr -d ' ')
DATA_BYTES=$(wc -c < "$DATA_FILE" | tr -d ' ')

echo
echo "Wrote $SCHEMA_FILE (${SCHEMA_BYTES} bytes), $DATA_FILE (${DATA_BYTES} bytes)"
echo "  CREATE TABLE statements : $SCHEMA_TABLES"

if [ "$SCHEMA_TABLES" -eq 0 ]; then
    echo
    echo "ERROR: no CREATE TABLE found in the schema file. The download is" >&2
    echo "probably an HTML error page rather than SQL. Check PAGILA_SCHEMA_URL." >&2
    exit 1
fi

echo
echo "Next: docker compose --profile pagila up -d pagila-db"
echo "(If the pagila_data volume already exists, run"
echo " 'docker compose --profile pagila down -v' first -- Postgres init"
echo " scripts only run on an empty data volume.)"
