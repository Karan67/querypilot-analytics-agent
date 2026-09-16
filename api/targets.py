"""The closed set of databases this deployment may talk to.

Lives outside `api.db` deliberately: `api/main.py` is asserted to reach the
database only through `execute_sql()` — a structural test walks its imports
and fails on anything else whose module name starts with `api.db` — and this
module holds no database code at all, just names, env-var keys and glossary
wiring. Being a sibling of `api.db` rather than a member of it means
`/databases` and `/ask`'s target validation can import this freely without
widening that test's allow-list for a module that never opens a connection.

**Named `api.targets`, not `api.db_targets`.** The obvious name was tried
first and rejected on measurement, not taste: the structural test's filter is
`module.startswith("api.db")`, a plain string prefix check with no package
boundary in it, and `"api.db_targets".startswith("api.db")` is `True` —
confirmed by running it, not by reasoning about it. A module meant to dodge
that test by living outside `api.db` would have tripped it anyway, for a
reason invisible from either file until someone actually ran the test.

**This is the registry the dynamic-database-switching feature's Architectural
Invariant #1 exists for**: a client sends one of these keys, never a DSN.
`api/db/engine.py` is the only module that turns a key into a connection, and
it refuses anything not listed here. The registry is closed on purpose —
growing the set of servable databases is one entry here, never a string a
client can invent.
"""

from __future__ import annotations

from typing import NamedTuple


class DatabaseTarget(NamedTuple):
    """One database this deployment can be pointed at.

    `dsn_env` names the environment variable holding its read-only DSN —
    never the DSN itself, which stays server-side per Invariant #1.

    `glossary_env` names the variable holding its glossary file path, or
    `None` when this target has no business-term vocabulary of its own.
    Pagila is not Chinook's music store, and shipping Chinook's glossary to
    it would be exactly the Chinook-shaped assumption
    `017-schema-generality.md` measured going wrong (a 43-row disagreement on
    "active customer" once the model was handed a definition written for a
    different schema's `active` column).
    """

    dsn_env: str
    glossary_env: str | None


#: name -> configuration, for every database this API may ever be asked to
#: answer against.
DATABASE_TARGETS: dict[str, DatabaseTarget] = {
    "chinook": DatabaseTarget(
        dsn_env="QUERYPILOT_DATABASE_URL",
        # The existing, single-target variable (Iteration 14) — reused
        # rather than duplicated, so a deployment that already points it at
        # chinook.json keeps working with no change.
        glossary_env="QUERYPILOT_GLOSSARY_FILE",
    ),
    "pagila": DatabaseTarget(
        dsn_env="QUERYPILOT_PAGILA_DATABASE_URL",
        # No glossary of its own: Pagila is the proof that the product works
        # without a supplied vocabulary, not a second Chinook.
        glossary_env=None,
    ),
}

#: The target used when a caller supplies none — every caller of
#: `get_schema()`, `execute_sql()`, `answer()` and `build_loop_system()` that
#: predates this feature, unchanged.
DEFAULT_TARGET = "chinook"

#: A client named a database outside `DATABASE_TARGETS` -- a request error
#: (`api/http/errors.py` maps it to 400), distinct from a *known* target
#: whose DSN is not configured on this deployment, which categorises as the
#: ordinary `connection_error` an unreachable database always has. Lives
#: here, not in `api/main.py`, so `api/http/errors.py` can import it without
#: `api/main.py` and `api/http/errors.py` importing each other.
CATEGORY_UNKNOWN_DATABASE = "unknown_database"
