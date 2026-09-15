"""Iteration 14 T3 (specs/017-schema-generality.md) -- the smoke set that
proves the engine against Pagila, a real Postgres schema this repo did not
shape and Chinook cannot express.

**Not `run_evals.py`.** That runner is built for the Chinook dataset's shape:
four difficulty tiers, a dev/test split, a glossary on/off arm, a quota
ledger. None of that applies to five hand-picked questions against a second
database whose only job is proving the loop survives a schema it has never
seen -- AC1, AC3 and AC5 of the spec, not a second accuracy benchmark (Q-C).

Every expected value below was executed against pagila-db directly
(`docker exec querypilot-pagila-db psql ...`) on 2026-09-15/16 before being
written here, the same discipline `api/agent/glossary.py` documents for its
own definitions: a number that does not reproduce its measured count is a
wrong number, not a wording preference.

Usage, against a running `pagila-db` (``docker compose --profile pagila up -d
pagila-db``)::

    python -m evals.pagila_smoke
"""

from __future__ import annotations

import datetime as dt
import pathlib

from api.agent.orchestrator import answer
from evals.run_evals import _load_dotenv

RESULTS_FILE = pathlib.Path(__file__).parent / "PAGILA_SMOKE.md"

#: (question, expected_row_count, expected_notes). `expected_notes` is a human
#: description of what a correct SQL result contains, checked by eye against
#: the printed rows rather than by a brittle string match -- these are smoke
#: questions, not scored gold queries.
QUESTIONS = [
    (
        "How many films are in the catalog?",
        1,
        "one row, count = 1000",
    ),
    (
        "How many actors are there?",
        1,
        "one row, count = 200",
    ),
    (
        "Which film has the longest length, and what is its rating?",
        1,
        "one row: GANGS PRIDE, PG-13, length 185",
    ),
    (
        "How many customers are active?",
        1,
        "one row, count = 966 (customer.active = 1)",
    ),
    (
        "What is the total amount collected from all payments?",
        None,
        (
            "deliberate stress case: payment is a partitioned table with 55 "
            "monthly children and no queryable parent relation, confirmed by "
            "direct introspection (api.db.introspection.get_schema() does not "
            "list 'payment', only 'payment_p*'). True total across every "
            "partition, summed by hand: 170962.39. Whether the agent notices "
            "it can only see partitions -- and how it responds -- is the "
            "finding this question exists to surface, not a pass/fail gate."
        ),
    ),
]


def run() -> None:
    lines = [
        "# Pagila smoke results",
        "",
        "Iteration 14 (`specs/017-schema-generality.md`), AC1/AC3/AC5. **Not**",
        "`EVALS.md` -- these numbers are never blended with Chinook's.",
        "",
        f"Run: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]

    for question, expected_rows, note in QUESTIONS:
        result = answer(question, glossary=False)
        lines.append(f"## {question}")
        lines.append("")
        lines.append(f"- Expected: {note}")
        lines.append(f"- `ok`: {result.ok}")
        lines.append(f"- SQL: `{result.sql}`")
        if result.ok and result.result is not None:
            lines.append(f"- Columns: {result.result.columns}")
            lines.append(f"- Rows returned: {len(result.result.rows)}")
            lines.append(f"- Rows: {result.result.rows}")
        else:
            lines.append(f"- Category: {result.category}")
            lines.append(f"- Error: {result.error}")
        lines.append(f"- Attempts used: {result.attempts_used}")
        lines.append("")

    RESULTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {RESULTS_FILE}")


if __name__ == "__main__":
    _load_dotenv()
    run()
