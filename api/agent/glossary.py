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
"""

from __future__ import annotations

#: Term -> definition, in schema terms.
#:
#: Definitions are written to be **acted on, not admired**: each names the
#: relations and columns that decide membership, because a definition the model
#: cannot translate into a WHERE clause has cost tokens and taught nothing.
#:
#: Measured at T3 with `tiktoken`/`o200k_base`: 168 tokens for the eight
#: definitions, 178 for the rendered block including its header -- against the
#: plan's §6 estimate of 220. Charged on **every** call (resolved Q-D), which at
#: 40 questions is 7,160 tokens per pass and very nearly cancels what schema
#: compaction saves. That trade is deliberate and is documented in the spec: a
#: glossary injected only for the questions that need it would tell the model
#: which questions are the ambiguous ones, which no real deployment could do.
GLOSSARY: dict[str, str] = {
    # 59 customers exist; 46 are active. Retired question `medium-008`'s sibling
    # ambiguity -- unfair without a stated convention, fair with one.
    "active customer": (
        "a customer with at least one invoice dated within 12 months of the "
        "most recent invoice_date in the database"
    ),
    # 8 employees exist; 3 are support representatives. This is precisely the
    # ambiguity that got `medium-008` retired in Iteration 3 as unfair.
    "support representative": (
        "an employee who appears as customer.support_rep_id for at least one "
        "customer; other employees are not"
    ),
    # 3503 tracks in the catalogue; 1984 have ever been purchased.
    "sold track": (
        "a track appearing in at least one invoice_line; a track never "
        "purchased is not one"
    ),
    # 275 artists exist; 165 have a track that has sold.
    "charting artist": (
        "an artist with at least one track that appears in an invoice_line, "
        "joined through album"
    ),
    # 25 genres exist; 24 have any sales. The narrowest margin of the eight, and
    # kept for exactly that reason -- a one-row difference is still a difference,
    # and a question that hinges on it cannot be answered by guessing the shape.
    "active genre": "a genre with at least one track that appears in an invoice_line",
    # 18 playlists exist; 14 contain at least one track.
    "curated playlist": (
        "a playlist containing at least one track; an empty playlist is not one"
    ),
    # 5.6519 per invoice against 1.0396 per line.
    #
    # **The one term whose direction the spec had backwards.** §2.4 originally
    # labelled the per-line figure "conventional"; an invoice_line is not an
    # order, and AOV universally means revenue per order. Corrected at T3 --
    # both measured numbers stand, only which one is the naive reading flipped.
    # Defining it the spec's original way would have taught a definition most
    # analysts would call wrong, and punished the correct instinct.
    "average order value": (
        "the mean of invoice.total across invoices; one order is one invoice, "
        "never one invoice_line"
    ),
    # 3503 tracks; 2526 name a composer.
    "credited track": "a track whose composer is not null",
}

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
        terms: override, for tests and for measuring a subset. Defaults to the
            whole `GLOSSARY`, because resolved Q-D injects all of it on every
            call regardless of what the question asks.
    """
    entries = GLOSSARY if terms is None else terms
    lines = [GLOSSARY_HEADER]
    lines += [f"- {term}: {definition}" for term, definition in entries.items()]
    return "\n".join(lines)
