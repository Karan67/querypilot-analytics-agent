#!/bin/bash
#
# Postgres init hook 2 of 2 - create the read-only role.
#
# This is Gate 1 of the safety layer described in specs/000-project.md section 4:
# the agent connects with a role that has no privilege to write, so even a
# perfect jailbreak of the model cannot modify the target database.
#
# Runs AFTER 01_load_chinook.sh by filename order, which is required:
# GRANT SELECT ON ALL TABLES IN SCHEMA public applies only to tables that exist
# at the moment the grant executes.
#
# A shell wrapper rather than a plain .sql file so the password comes from the
# environment instead of being committed. The SQL below is otherwise the role
# definition from the project spec, section 6.

set -euo pipefail

: "${QUERYPILOT_RO_USER:?QUERYPILOT_RO_USER must be set (see .env.example)}"
: "${QUERYPILOT_RO_PASSWORD:?QUERYPILOT_RO_PASSWORD must be set (see .env.example)}"
: "${QUERYPILOT_STATEMENT_TIMEOUT:=10s}"

echo "[querypilot] Creating read-only role '$QUERYPILOT_RO_USER' on '$POSTGRES_DB'..."

# psql interpolation: :"name" quotes as an identifier, :'name' as a literal.
# Both escape correctly, so neither the role name nor the password can break
# out of its position in the statement.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --quiet \
     -v ro_user="$QUERYPILOT_RO_USER" \
     -v ro_password="$QUERYPILOT_RO_PASSWORD" \
     -v stmt_timeout="$QUERYPILOT_STATEMENT_TIMEOUT" \
     -v db_name="$POSTGRES_DB" <<'EOSQL'

CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_password';

GRANT CONNECT ON DATABASE :"db_name" TO :"ro_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";

-- The statement above is a ONE-TIME SNAPSHOT over relations that exist right
-- now, not a standing rule. Verified in T1: every relation created after the
-- grant was denied to the role. That is why 02_create_views.sql must run
-- before this file.
--
-- ALTER DEFAULT PRIVILEGES covers the other direction - relations created
-- later. It is not retroactive, so it complements the snapshot grant rather
-- than replacing it; both are required. T1 confirmed it covers views and
-- materialized views, not just ordinary tables.
--
-- Applies to relations created by the role running this script (POSTGRES_USER),
-- which is how every relation in this database is created.
--
-- Consequence worth knowing: any relation added to public later becomes
-- agent-readable automatically. That is correct for an analytics database, but
-- it means a future table is readable by default rather than invisible by
-- default. If sensitive data is ever loaded into this schema, that assumption
-- has to be revisited here.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"ro_user";

-- Gate 3 (resource level), pinned to the role so every session this role opens
-- starts with a timeout, including pooled connections the API reopens later.
--
-- Read this before relying on it: ALTER ROLE ... SET installs a per-session
-- DEFAULT, not a hard ceiling. The role itself can raise it with
--     SET statement_timeout = 0;
-- That is tolerable here for exactly one reason: Gate 2 rejects anything that
-- is not a single SELECT, so the agent has no route to issue a SET at all.
-- Gates 2 and 3 hold this together jointly. If Gate 2 is ever loosened to
-- permit additional statement types, this timeout stops being an enforced
-- limit and the resource gate has to move into the session or the pool.
ALTER ROLE :"ro_user" SET statement_timeout = :'stmt_timeout';

EOSQL

# Verify the gate rather than assume it. If any privilege other than SELECT
# ever reaches this role, startup fails loudly instead of shipping a database
# the agent can write to.
NON_SELECT=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    -v ro_user="$QUERYPILOT_RO_USER" <<'EOSQL'
SELECT count(*)
FROM information_schema.table_privileges
WHERE grantee = :'ro_user'
  AND privilege_type <> 'SELECT';
EOSQL
)

GRANTED=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    -v ro_user="$QUERYPILOT_RO_USER" <<'EOSQL'
SELECT count(DISTINCT table_name)
FROM information_schema.table_privileges
WHERE grantee = :'ro_user'
  AND privilege_type = 'SELECT';
EOSQL
)

echo "[querypilot] Role '$QUERYPILOT_RO_USER': SELECT on $GRANTED tables, $NON_SELECT non-SELECT privileges."

if [ "$NON_SELECT" -ne 0 ]; then
    echo "ERROR: read-only role holds $NON_SELECT non-SELECT table privileges." >&2
    exit 1
fi

if [ "$GRANTED" -eq 0 ]; then
    echo "ERROR: read-only role was granted SELECT on zero tables. Did the" >&2
    echo "dataset load before this script ran?" >&2
    exit 1
fi

# Read the role default back rather than trusting that the ALTER succeeded.
ROLE_CONFIG=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    -v ro_user="$QUERYPILOT_RO_USER" <<'EOSQL'
SELECT coalesce(array_to_string(rolconfig, ', '), '')
FROM pg_roles
WHERE rolname = :'ro_user';
EOSQL
)

echo "[querypilot] Role defaults: ${ROLE_CONFIG:-<none>}"

# AC12 as a startup assertion, not a one-off check: every relation the schema
# tool can see must also be readable by the role. A relation the agent can see
# but cannot read is worse than one it cannot see at all - it yields a query
# that passes validation and then dies at execution, which the agent cannot
# self-correct out of. relkind r/v/m = table / view / materialized view.
UNREADABLE=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    -v ro_user="$QUERYPILOT_RO_USER" <<'EOSQL'
SELECT coalesce(string_agg(c.relname, ', ' ORDER BY c.relname), '')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'v', 'm')
  AND NOT has_table_privilege(:'ro_user', c.oid, 'SELECT');
EOSQL
)

if [ -n "$UNREADABLE" ]; then
    echo "ERROR: these public relations are NOT readable by $QUERYPILOT_RO_USER:" >&2
    echo "         $UNREADABLE" >&2
    echo "       Anything created after the GRANT in this file needs to move" >&2
    echo "       ahead of it. See db/init/02_create_views.sql." >&2
    exit 1
fi

echo "[querypilot] All public relations readable by '$QUERYPILOT_RO_USER'."

case "$ROLE_CONFIG" in
    *statement_timeout*) ;;
    *)
        echo "ERROR: statement_timeout was not pinned to the role." >&2
        exit 1
        ;;
esac
