# Seeding Neon (specs/019-production-deployment.md, Q-A/AC6-AC8)

This is a **one-time manual procedure**, not a script. Neon is a managed
Postgres service — nothing under `db/init/` or `db/init-pagila/` runs against
it, because those only execute as Docker's
`docker-entrypoint-initdb.d` convention on a fresh, locally-created volume,
which a managed database never is. Run the steps below by hand, once, against
a freshly created project. Re-running them against a database that already
has data in it is your own risk, exactly as re-running `db/init/` by hand
against a live local database would be.

You need `psql` on your own machine (the same client `db/init/*.sh` already
use inside the containers) and this repository checked out, so the seed
files and SQL below are available locally.

## 1. Create the Neon project

In the Neon console: **New Project**, Postgres version **18** (needed for
`pagila`'s `uuidv7()` default columns — confirmed supported and the default
for new projects as of mid-2026). One project holds both databases
(`specs/019` §7 Q-B) — this is an isolation preference, not a capacity limit;
Neon's free tier allows up to 100 projects, so a second project is equally
free if you'd rather keep the two datasets apart.

Note the project's default connection string — you'll use it once, as the
project's superuser, to create the two databases and the read-only role.
It is **not** one of the two values this deployment ends up using.

## 2. Create the two databases

In the Neon console's **Databases** tab (or `neonctl databases create`),
create:

- `chinook`
- `pagila`

## 3. Seed `chinook`

Fetch the dataset locally first, the same way local development does:

```bash
./db/fetch_chinook.sh          # or db\fetch_chinook.ps1 on Windows
```

That produces `db/seed/chinook.sql`. Load it, then the views file, against
the `chinook` database's own connection string (not the project default —
Neon gives each database its own connection string once created):

```bash
psql "<chinook connection string>" -v ON_ERROR_STOP=1 --quiet --file db/seed/chinook.sql
psql "<chinook connection string>" -v ON_ERROR_STOP=1 --quiet --file db/init/02_create_views.sql
```

This is the same `psql -f` invocation `db/init/01_load_chinook.sh` runs
inside the container, and the same view file `db/init/02_create_views.sql`
run second, for the same reason the local init scripts run it second:
`GRANT SELECT ON ALL TABLES` is a snapshot, and a view created after the
grant would be visible to `get_schema()` and unreadable to the query that
follows it.

## 4. Seed `pagila`

```bash
./db/fetch_pagila.sh           # or db\fetch_pagila.ps1 on Windows
```

That produces `db/seed/pagila-schema.sql` and `db/seed/pagila-data.sql`
(schema and data are separate files, unlike Chinook's single dump — see
`db/init-pagila/01_load_pagila.sh`'s header). Load both, in order, against
the `pagila` database's connection string:

```bash
psql "<pagila connection string>" -v ON_ERROR_STOP=1 --quiet --file db/seed/pagila-schema.sql
psql "<pagila connection string>" -v ON_ERROR_STOP=1 --quiet --file db/seed/pagila-data.sql
```

## 5. Create the read-only role, on each database

This is the SQL `db/init/03_readonly_role.sh` runs as a heredoc — reproduced
here literally rather than run as that script, because the script also reads
`$POSTGRES_USER`/`$POSTGRES_DB` from the container's own environment, which
does not exist outside it. Pick your own role name and password (these
become `QUERYPILOT_RO_USER`/`QUERYPILOT_RO_PASSWORD` in spirit, though on
Neon they only need to exist in the connection string itself, not as
separate variables), and run this once per database — `chinook` and
`pagila` each need their own role, matching how `db` and `pagila-db` each
provision their own locally:

```sql
CREATE ROLE querypilot_ro LOGIN PASSWORD '<a password you choose>';

GRANT CONNECT ON DATABASE <chinook_or_pagila> TO querypilot_ro;
GRANT USAGE ON SCHEMA public TO querypilot_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO querypilot_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO querypilot_ro;
ALTER ROLE querypilot_ro SET statement_timeout = '10s';
```

Run against `chinook`'s connection string with `<chinook_or_pagila>` set to
`chinook`, and again against `pagila`'s connection string with it set to
`pagila`. This is Gate 1 of the safety layer (`specs/000-project.md` §4): the
deployed API only ever holds this role's credential, never the project
superuser's.

**Verify it before moving on** — the same check `03_readonly_role.sh` runs
automatically, run here by hand:

```sql
SELECT count(*) FROM information_schema.table_privileges
WHERE grantee = 'querypilot_ro' AND privilege_type <> 'SELECT';
-- must be 0

SELECT count(DISTINCT table_name) FROM information_schema.table_privileges
WHERE grantee = 'querypilot_ro' AND privilege_type = 'SELECT';
-- must be > 0
```

## 6. Fill in Render's environment variables

Two connection strings, each with `querypilot_ro`'s credential (not the
project superuser's) substituted in, become the two secret-shaped variables
`render.yaml` declares:

| Neon database | Render env var |
|---|---|
| `chinook`, role `querypilot_ro` | `QUERYPILOT_DATABASE_URL` |
| `pagila`, role `querypilot_ro` | `QUERYPILOT_PAGILA_DATABASE_URL` |

Both are `postgresql+psycopg://` DSNs, the same driver prefix
`docker-compose.yml`'s `api` service already uses locally.

## 7. After the first live deploy

Check `QUERYPILOT_TRUSTED_PROXIES` (`render.yaml` ships it unset, trusting
only loopback and RFC-1918 addresses by default — see `specs/019-production-
deployment-plan.md` D-3). If the first live request's `Strict-Transport-
Security` header is missing when it should be present, Render's edge is
likely presenting a forwarded-header shape this default doesn't recognize,
and the variable needs a value naming it.
