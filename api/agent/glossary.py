"""Business term definitions — `specs/008-prompt-tuning.md` AC10, AC11.

**Definitions only.** The prompt mechanics that inject this block live in
`api/agent/prompts.py`; this module is the domain content, kept separate for the
same reason `tools.py` is a registry rather than an implementation
(`000-project.md` §5). Wording here is expected to churn against eval numbers,
and it should not churn inside the code that assembles prompts.

### Why these eight, and why they are populations

Spec §2.4 measured ten candidate terms by executing both readings of each. Two
were discarded because they do not discriminate: `sum(invoice.total)` and
`sum(line.unit_price * quantity)` are both 2328.60, so the textbook "what does
revenue mean" trap is untestable on Chinook, and albums-with-a-track equals all
albums at 347.

What survives is **population definition** — which rows count as a customer, a
track, an artist (AC11). That is a different kind of domain knowledge from
metric definition, and it is the kind this schema supports.

### Every definition below was executed before it was written down

Each term states the row count its conventional reading returns and the count a
naive reading returns, verified at T3 through `execute_sql()` against the live
database. A definition that does not reproduce its measured number is a wrong
definition rather than a wording preference, and `tests/test_glossary.py`
re-executes all sixteen queries so the pair cannot silently converge if the
database is reseeded.

### Iteration 14: the eight terms moved into `api/glossary/chinook.json`

`specs/017-schema-generality.md` AC2. A deployment pointed at a database this
repo did not seed has no business reason to carry Chinook's music-store
vocabulary into every prompt — measured directly against Pagila (`evals/
PAGILA_SMOKE.md`): the model answered "how many customers are active" using a
different, equally real `activebool` column instead of the `active` one a
Chinook-shaped assumption would reach for, a 43-row disagreement that only a
supplied definition can settle. So the terms now live in a file,
`QUERYPILOT_GLOSSARY_FILE` says where to find it, and Chinook's own eight are
that file's shipped content rather than a code-level default no other
deployment can opt out of (resolved Q-B: unset means empty, not Chinook's).

The measured counts that justified each term, preserved here since the JSON
file itself cannot hold a comment:

- **active customer** — 59 customers exist; 46 are active. Retired question
  `medium-008`'s sibling ambiguity: unfair without a stated convention, fair
  with one.
- **support representative** — 8 employees exist; 3 are support
  representatives. Precisely the ambiguity that got `medium-008` retired in
  Iteration 3 as unfair.
- **sold track** — 3503 tracks in the catalogue; 1984 have ever been
  purchased.
- **charting artist** — 275 artists exist; 165 have a track that has sold.
- **active genre** — 25 genres exist; 24 have any sales. The narrowest margin
  of the eight, kept for exactly that reason: a one-row difference is still a
  difference, and a question that hinges on it cannot be answered by guessing
  the shape.
- **curated playlist** — 18 playlists exist; 14 contain at least one track.
- **average order value** — 5.6519 per invoice against 1.0396 per line. The
  one term whose direction the spec had backwards: an invoice_line is not an
  order, and AOV universally means revenue per order. Corrected at T3 after
  the spec originally labelled the per-line figure "conventional" — both
  measured numbers stand, only which one is the naive reading flipped.
- **credited track** — 3503 tracks; 2526 name a composer.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

from api.config import read_secret_file
from api.targets import DATABASE_TARGETS, DEFAULT_TARGET

logger = logging.getLogger("querypilot")

#: The environment variable naming a glossary file to load, term ->
#: definition, as JSON. Unset (or a file that cannot be read or parsed) means
#: no glossary at all (D-2, fail open) — this is domain guidance, not a
#: safety gate, so a typo here costs accuracy, never availability.
GLOSSARY_FILE_ENV = "QUERYPILOT_GLOSSARY_FILE"

#: Chinook's own glossary, shipped as data rather than code so it is loaded
#: through the exact same path any other deployment's file would be — see
#: `docker-compose.yml`, which points the demo's `QUERYPILOT_GLOSSARY_FILE`
#: here by default, and `tests/conftest.py`, which does the same for the
#: suite.
CHINOOK_GLOSSARY_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "glossary" / "chinook.json"
)

#: Term -> definition, in schema terms, loaded once at import.
#:
#: Definitions are written to be **acted on, not admired**: each names the
#: relations and columns that decide membership, because a definition the
#: model cannot translate into a WHERE clause has cost tokens and taught
#: nothing.
#:
#: Measured at T3 with `tiktoken`/`o200k_base`: 168 tokens for the eight
#: definitions, 178 for the rendered block including its header. Kept
#: identical by keeping `chinook.json`'s content identical to what used to be
#: a Python literal here — this module-level load exists so
#: `tests/test_glossary.py`'s `from api.agent.glossary import GLOSSARY` still
#: has something to import; the *deployed* prompt path no longer reads this
#: constant at all (see `current_glossary_terms` below).
GLOSSARY: dict[str, str] = json.loads(CHINOOK_GLOSSARY_PATH.read_text(encoding="utf-8"))

#: Introduces the block. **"not your own" is load-bearing**: the terms below are
#: ordinary English whose everyday meaning is exactly the naive reading each one
#: is trying to displace, so the instruction has to say that the stated
#: definition overrides prior belief rather than merely informing it.
GLOSSARY_HEADER = "Business terms — use these definitions, not your own:"


def render_glossary(terms: dict[str, str] | None = None) -> str:
    """The glossary as a prompt block.

    Deterministic: `dict` preserves insertion order, and nothing here re-sorts.
    Two calls produce the same string, which is what keeps a prompt diffable
    between eval runs (`001` AC13) and a fingerprint stable.

    Args:
        terms: override, for tests and for measuring a subset. Defaults to
            Chinook's own `GLOSSARY` — this default is what
            `tests/test_glossary.py` exercises directly; the deployed prompt
            path goes through `current_glossary_terms()` instead (Iteration
            14), which reads `QUERYPILOT_GLOSSARY_FILE` rather than this
            constant.
    """
    entries = GLOSSARY if terms is None else terms
    lines = [GLOSSARY_HEADER]
    lines += [f"- {term}: {definition}" for term, definition in entries.items()]
    return "\n".join(lines)


def _load_glossary_terms(path: str) -> dict[str, str]:
    """Parse one glossary file into term -> definition, or `{}` having logged why.

    **Fails open, deliberately unlike `api/http/auth.py::load_identities`.**
    An unreadable or malformed credential map is a security hole and refuses
    every request; an unreadable or malformed glossary file is a missing
    piece of domain guidance, and refusing to answer *because a business-term
    file had a typo* would make a config mistake outrank a safety mistake in
    how hard it fails. So every branch below returns `{}` rather than raising,
    each logged once with what was wrong and never with the file's contents.

    `config.read_secret_file` already logs a missing, unreadable or
    non-UTF-8 path without this function repeating that branch; only "valid
    file, invalid JSON" and "JSON is not a flat string-to-string object" are
    new here.
    """
    if not path.strip():
        return {}

    raw = read_secret_file(path)
    if raw is None:
        # Already logged by read_secret_file with the reason (missing,
        # unreadable, not UTF-8).
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # `str(exc)` only, matching `auth.load_identities`'s discipline: never
        # interpolate the raw document, which could echo file contents into
        # the log.
        logger.warning("%s (%s) is not valid JSON: %s", GLOSSARY_FILE_ENV, path, exc)
        return {}

    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        logger.warning(
            "%s (%s) must be a JSON object of term to definition, both strings.",
            GLOSSARY_FILE_ENV,
            path,
        )
        return {}

    return parsed


#: The last (target, raw path) seen and what it parsed to. Keyed on both —
#: the same shape as `api/http/auth.py`'s `_memo` widened by one field — so a
#: changed env var *or* a different target re-reads, and a rotated glossary
#: needs no restart.
_memo: tuple[str, str, dict[str, str]] | None = None


def current_glossary_terms(target: str = DEFAULT_TARGET) -> dict[str, str]:
    """The glossary terms one target's questions should be answered with.

    Never raises — an unrecognised `target` behaves exactly like every other
    misconfiguration this function already tolerates: `{}`, not an error, per
    D-2's fail-open rule below.

    **Glossary coupling** (dynamic-database-switching, Invariant #3): each
    registered target names its own glossary variable in
    `api.targets.DATABASE_TARGETS`, or `None` for a target with no
    business-term vocabulary of its own — Pagila, deliberately, per that
    registry's own docstring. Chinook keeps reading the pre-existing
    `QUERYPILOT_GLOSSARY_FILE` (resolved Q-B, Iteration 14): unset means no
    glossary, not Chinook's by default, and the demo's own
    `docker-compose.yml` already points that variable at `chinook.json`,
    which is what keeps the shipped demo's prompt unchanged from before
    targets existed.
    """
    global _memo

    entry = DATABASE_TARGETS.get(target)
    glossary_env = entry.glossary_env if entry is not None else None
    if glossary_env is None:
        return {}

    path = os.environ.get(glossary_env, "")
    if _memo is not None and _memo[0] == target and _memo[1] == path:
        return _memo[2]

    terms = _load_glossary_terms(path)
    _memo = (target, path, terms)
    return terms


def reset_glossary_cache() -> None:
    """Forget the memo — for a test that wants the next call to re-read."""
    global _memo

    _memo = None
