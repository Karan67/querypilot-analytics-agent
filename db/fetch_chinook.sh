#!/usr/bin/env bash
#
# Fetch the Chinook PostgreSQL dump into db/seed/chinook.sql.
#
# Run this on the HOST, once, before the first `docker compose up`. The dump is
# not committed to the repo; db/init/01_load_chinook.sh loads whatever this
# script leaves behind.
#
#   ./db/fetch_chinook.sh            # no-op if the seed already exists
#   ./db/fetch_chinook.sh --force    # re-download
#
# Override the source with CHINOOK_SQL_URL if the upstream path ever moves.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_DIR="${SCRIPT_DIR}/seed"
SEED_FILE="${SEED_DIR}/chinook.sql"
RAW_FILE="${SEED_DIR}/.chinook.raw.sql"

CHINOOK_SQL_URL="${CHINOOK_SQL_URL:-https://raw.githubusercontent.com/lerocha/chinook-database/master/ChinookDatabase/DataSources/Chinook_PostgreSql.sql}"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ -f "$SEED_FILE" ] && [ "$FORCE" -eq 0 ]; then
    echo "Seed already present: $SEED_FILE"
    echo "Re-download with: $0 --force"
    exit 0
fi

mkdir -p "$SEED_DIR"

echo "Downloading Chinook from:"
echo "  $CHINOOK_SQL_URL"

if command -v curl >/dev/null 2>&1; then
    curl --fail --location --show-error --silent -o "$RAW_FILE" "$CHINOOK_SQL_URL"
elif command -v wget >/dev/null 2>&1; then
    wget --quiet -O "$RAW_FILE" "$CHINOOK_SQL_URL"
else
    echo "ERROR: neither curl nor wget is available." >&2
    exit 1
fi

# The upstream dump manages its own database: it opens with
#   DROP DATABASE IF EXISTS chinook; CREATE DATABASE chinook; \c chinook
# Inside the Postgres init hook we are already connected to POSTGRES_DB, and a
# session cannot drop the database it is connected to. Strip that preamble so
# the file loads into whichever database psql is pointed at. Everything after
# it (CREATE TABLE / INSERT) is untouched.
#
# awk rather than `sed -I` because BSD sed has no case-insensitive addresses.
awk '
    {
        lower = tolower($0)
        if (lower ~ /^[[:space:]]*(drop|create)[[:space:]]+database[[:space:]]/) next
        if (lower ~ /^[[:space:]]*\\(c|connect)([[:space:]]|;|$)/) next
        print
    }
' "$RAW_FILE" > "$SEED_FILE"

rm -f "$RAW_FILE"

# The trailing [[:space:]] is required, not incidental: it distinguishes a real
# statement ("CREATE DATABASE chinook;") from the comment line "   Create
# database" in the upstream file header, which is harmless and must not be
# reported as leftover.
STRIPPED=$(grep -ic '^[[:space:]]*\(drop\|create\)[[:space:]]\+database[[:space:]]' "$SEED_FILE" || true)
TABLES=$(grep -ic '^[[:space:]]*create[[:space:]]\+table' "$SEED_FILE" || true)
BYTES=$(wc -c < "$SEED_FILE" | tr -d ' ')

echo
echo "Wrote $SEED_FILE (${BYTES} bytes)"
echo "  CREATE TABLE statements : $TABLES"
echo "  leftover CREATE/DROP DATABASE : $STRIPPED (expected 0)"

if [ "$TABLES" -eq 0 ]; then
    echo
    echo "ERROR: no CREATE TABLE found. The download is probably an HTML error" >&2
    echo "page rather than SQL. Check CHINOOK_SQL_URL." >&2
    exit 1
fi

echo
echo "Next: docker compose up --build"
echo "(If the db volume already exists, run 'docker compose down -v' first --"
echo " Postgres init scripts only run on an empty data volume.)"
