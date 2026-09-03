"""The agent's tool surface.

**This module is a registry, not an implementation.** Per
`specs/000-project.md` §5, every tool appears here as a thin wrapper and
delegates to its own domain module — `get_schema()` to `api/db/introspection.py`,
`validate_sql()` to `api/safety/validator.py` when it lands. The point is that
by Iteration 4 a reader can open this one file and see every capability the
agent has, without it having decayed into a grab bag of unrelated logic.

Deliberately minimal — decision D-2 in `specs/001-schema-tool-plan.md`. There is
no JSON-schema generation and no dispatch-on-model-output here, because tool
schemas are **provider-shaped**: Groq, Anthropic and Gemini each describe tools
differently, so serialisation belongs behind the LLM interface rather than in
the shared tool surface. It cannot be designed before `specs/007-agent-loop.md`
picks the provider contract, and inventing it now would bake one provider's
shape into a module every provider has to use.

Iterations 1–3 read `TOOLS` as documentation. Iteration 4 adds the calling
convention on top of it.
"""

from __future__ import annotations

from collections.abc import Callable

from api.db.execution import ExecutionResult
from api.db.execution import execute_sql as _execute_sql
from api.db.introspection import Schema
from api.db.introspection import get_schema as _introspect_schema
from api.safety.validator import validate_sql as _validate_sql


def get_schema() -> Schema:
    """Return the structural map of the target database.

    Takes no arguments, and must keep taking none: that is what leaves the tool
    with no injection surface (AC14 in `specs/001-schema-tool.md`).

    Raises:
        SchemaIntrospectionError: if the catalog cannot be read. Propagated
            unchanged — the agent needs the underlying reason in order to react,
            and flattening it here would cost exactly the detail that makes it
            actionable.
    """
    return _introspect_schema()


def validate_sql(sql: str) -> tuple[bool, str]:
    """Check a candidate query against Gate 2 of the safety layer.

    Delegates to `api.safety.validator.validate_sql`. See
    `specs/002-sql-validation.md`.

    Returns:
        `(True, "")` if the query is exactly one read-only SELECT, otherwise
        `(False, reason)`. The reason is written to be acted on: from Iteration
        4 the agent feeds it back into the loop and retries, so it is passed
        through unchanged rather than summarised here.

    Never raises, on any input (AC22).
    """
    return _validate_sql(sql)


def execute_sql(sql: str) -> ExecutionResult:
    """Run one read-only query and return its rows, or a categorised failure.

    Delegates to `api.db.execution.execute_sql`. See
    `specs/003-execute-sql.md`.

    This is the only tool that reaches the database with model output, and it
    validates before it does: Gate 2 runs inside the implementation, not in the
    caller, so there is no ordering for a caller to get wrong and no argument
    that skips it.

    Returns:
        An `ExecutionResult`. Failures are returned, never raised, and carry a
        `category` the retry loop can branch on -- `rejected`, `timeout`,
        `database_error`, `connection_error`, or `gate_violation`.
    """
    return _execute_sql(sql)


#: The agent's complete tool surface, name to callable.
#:
#: Names are the ones the model will use, so they are part of the contract:
#: renaming a key is a prompt change, not a refactor.
#:
#: **`sample_rows` was removed at Iteration 5 T1** (`008-prompt-tuning.md` AC1).
#: Not because it was unsafe: it was chosen zero times across 24 probe calls and
#: every recorded Iteration 4 run, and an option the model never takes is dead
#: weight in its decision space. Measured, the prompt description cost 19 tokens.
#:
#: **Removing this entry is necessary but was not sufficient.** T1 measured that
#: the orchestrator dispatched the action through a direct import rather than
#: through this registry, so deleting the entry alone left the capability fully
#: live (spec AC4b). The dispatch branch had to go too. The lesson generalises:
#: this dictionary is the *declared* surface, and a declared surface only
#: constrains what the loop can do while the loop actually consults it.
#:
#: `api/db/sampling.py` and its tests are deliberately **kept** (resolved Q-B).
#: Deleting them buys zero tokens and would discard the only mechanism by which
#: the agent could ever inspect a value's format.
TOOLS: dict[str, Callable[..., object]] = {
    "get_schema": get_schema,
    "validate_sql": validate_sql,
    "execute_sql": execute_sql,
}
