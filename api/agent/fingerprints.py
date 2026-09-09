"""Hashes that say *which* prompt and *which* schema produced a number.

Two callers want fingerprints for opposite reasons, and this module holds the
one definition they share.

`evals/run_evals.py` has fingerprinted its prompts since Iteration 4, so that an
`EVALS.md` row records the configuration that produced it and an incomparable
number cannot be filed as a comparable one. Iteration 7's answer cache wants the
same hash for a different job: a cache key must change when the prompt changes,
or a process that outlives an edit serves answers built by a prompt that no
longer exists.

**The recipe moved here rather than being copied** (asked and answered at T4).
`evals/` is not in the Docker build context — the context is `./api` — so an API
module importing `evals.run_evals` would pass every test on the host and raise
`ModuleNotFoundError` in the container. Copying the recipe instead would leave
two definitions of one idea, free to drift in the direction nothing fails: a
prompt edit could move the cache key and not the recorded fingerprint, and the
first symptom would be an `EVALS.md` row that looks comparable and is not.

The hashed material is unchanged by the move, so the values pinned in the suite
(`0d280c367c5e` for the loop, `f971d8787f0c` for single-shot) still hold. Those
assertions are the proof, not this paragraph.

**What is deliberately *not* shared is the schema fingerprint**, because the two
callers mean different things by it and only one of them is about the schema:

- `evals.run_evals.schema_fingerprint` hashes a small hand-built schema to
  identify the *renderer*. Folding the live database in would make a reseed
  masquerade as a prompt change, and reseeds are already caught by the dataset's
  row-count fingerprint.
- `live_schema_fingerprint` below hashes the *actual rendered schema* being sent
  to the model, because a cache key exists to protect correctness: if a column
  is added, every cached answer was derived from a schema that no longer
  describes the database.

Same word, different quantities. A history row's `schema_fp` will therefore not
equal an `EVALS.md` `schema_fingerprint`, and that is correct rather than a bug
to reconcile later.
"""

from __future__ import annotations

import hashlib
import inspect

from api.agent.glossary import render_glossary
from api.agent.prompts import (
    ADOPTED_RENDERING,
    LOOP_SYSTEM_TEMPLATE,
    Schema,
    render_schema,
    render_transcript,
)
from api.db.introspection import SchemaIntrospectionError, get_schema

#: Characters of the hash kept. Twelve hex digits is 48 bits -- unambiguous for
#: a handful of prompt versions and short enough to read in a table.
FINGERPRINT_LENGTH = 12


def fingerprint(material: str) -> str:
    """Hash whatever identifies a configuration.

    **Derived, not declared.** The alternative -- a version constant somebody
    bumps -- fails in exactly the situation that matters: the prompt changes,
    the constant does not, and every number afterwards is filed under the wrong
    version. A number recorded against the wrong prompt is worse than one
    recorded against none, because it looks comparable.
    """
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def loop_prompt_fingerprint(*, glossary: bool) -> str:
    """The agent loop's prompt fingerprint.

    **Hashes the template plus the source of `render_transcript`**, per `009`
    D-1. The loop's behaviour depends on how failures are fed back as much as on
    the instructions, so a template-only hash would let a change to the feedback
    format produce a different number under an identical fingerprint. The cost
    is that editing a comment in that function changes the fingerprint, which is
    a far cheaper failure than the one it rules out.

    **The glossary is folded in, unlike the schema rendering**, and the asymmetry
    is deliberate: a rendering change leaves the DDL prompt byte-identical, so
    churning the hash would signal a change that did not happen, whereas the
    glossary genuinely adds 178 tokens to every call. A glossary-on run is not
    the same prompt as Iteration 4's.

    `glossary` is **keyword-only and has no default**. It used to default to
    `False`, which was right for the eval harness -- that is Iteration 4's
    configuration and reproduces the recorded value -- and is wrong for the
    deployed API, which ships the glossary on. A default here would silently
    record one caller's answers under the other's fingerprint.
    """
    material = LOOP_SYSTEM_TEMPLATE + inspect.getsource(render_transcript)
    if glossary:
        material += render_glossary()
    return fingerprint(material)


def live_schema_fingerprint(
    schema: Schema, rendering: str = ADOPTED_RENDERING
) -> str:
    """A hash of the schema **as the model will actually see it**.

    Hashes the rendered text rather than the `Schema` object, so it moves when
    either the database or the renderer moves -- both of which change what the
    model was told, which is the only thing a cached answer depended on.

    The rendering is folded in by name as well, so two renderings that happen to
    produce identical text for a trivial schema still fingerprint apart.
    """
    return fingerprint(f"{rendering}\n{render_schema(schema, rendering)}")


def deployed_fingerprints(
    *, rendering: str, glossary: bool
) -> tuple[str, str] | None:
    """`(schema_fp, prompt_fp)` for a live configuration, or `None`.

    **This function is why the schema read lives in `api/agent/` rather than in
    the endpoint.** `api/main.py` is asserted to reach the database only through
    the agent -- a structural test walks its imports and fails on anything from
    `api.db` -- and that assertion is worth more than the convenience of calling
    `get_schema()` where the cache key is assembled. Reading the schema to build
    a prompt is what this package already does in `orchestrator.py`; the caller
    supplies the configuration and gets back two numbers.

    `None` when the schema cannot be read. The caller is expected to carry on
    without a key rather than substitute a placeholder: a key built without a
    schema would collide across schemas, which is the one thing it exists to
    prevent. `answer()` then reports the unreachable database itself, in the
    category it already uses.
    """
    try:
        schema = get_schema()
    except SchemaIntrospectionError:
        return None

    return (
        live_schema_fingerprint(schema, rendering),
        loop_prompt_fingerprint(glossary=glossary),
    )
