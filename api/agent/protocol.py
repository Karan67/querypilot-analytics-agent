"""The agent's action protocol — parsing only, no I/O.

Implements `specs/007-agent-loop.md` AC14/AC19 and the format measured in
`specs/007-agent-loop-plan.md` §2. Resolved Q-B chose a text protocol over
vendor tool schemas, so that `api/llm/base.py` can stay one method and swapping
provider stays a configuration change.

The format::

    ACTION: execute_sql
    SELECT count(*) FROM track

**The first line names the action; everything after it is the argument,
opaque.** That rule is the whole design. SQL is free text and may contain the
literal string ``ACTION:`` inside a string literal, so a parser that scanned for
markers anywhere in the response would be ambiguous by construction -- and
ambiguous in the direction that lets a value in the data decide what runs.

Parsing here is **dispatch, not a safety decision** (AC19). Safety is Gate 2 and
the `TOOLS` registry: the worst a malformed or hostile action can do is name a
tool that does not exist, or produce SQL that `execute_sql` then refuses. This
module resolves nothing dynamically -- no `getattr`, no import by name -- so an
action called `__import__` is simply an unknown action.
"""

from __future__ import annotations

from dataclasses import dataclass

# `extract_sql` is imported rather than reimplemented. It strips `<think>`
# blocks and markdown fences, both of which are model-shaped noise this parser
# would otherwise have to know about twice -- and `005` measured a model that
# inlines reasoning into content, so the stripping is not optional. Importing
# from `single_shot` does not modify it (AC1); the dependency runs one way.
from api.agent.single_shot import extract_sql

#: The marker that opens an action line. Compared case-insensitively.
ACTION_PREFIX = "ACTION:"

#: What a response with no action line is taken to mean (plan §2.4).
#:
#: Measured compliance was 24/24 on the default model, so this is a safety net
#: rather than a main path. It exists because `005` AC3 makes the model an
#: environment variable, and `005` measured *this* model emitting bare SQL under
#: the single-shot prompt. A non-complying model therefore degrades to
#: single-shot behaviour instead of failing every turn on a parse error.
#:
#: Safe by construction: the argument goes to `execute_sql`, which validates
#: first. Prose reaches Gate 2 and is refused, which is the measured `005`
#: behaviour today.
DEFAULT_ACTION = "execute_sql"


@dataclass(frozen=True)
class Action:
    """One parsed action.

    `explicit` records whether an `ACTION:` line was actually present, and it is
    **not cosmetic**. It is the only structural signal separating "the model
    tried to act and got the SQL wrong" from "the model was not writing SQL at
    all" -- which is how a refusal is told apart from a mistake without reading
    a single word of the content. See `orchestrator.py`, which uses it to keep
    AC13 honest.
    """

    name: str
    argument: str
    explicit: bool


def parse_action(response: object) -> Action:
    """Parse a model response into an action and its argument.

    Args:
        response: raw model output. Any type; a non-string yields the default
            action with an empty argument rather than raising.

    Returns:
        An `Action`. **Never raises**, on any input (AC5 applies to the whole
        loop, and a parser that threw would end a run over a formatting slip).

    The action name is normalised -- trimmed and lowercased -- and validated
    nowhere. Whether it names a real tool is `TOOLS`' business, and keeping that
    decision in one place is what makes the registry the allow-list rather than
    one of two places that both have to be right.
    """
    if not isinstance(response, str):
        return Action(DEFAULT_ACTION, "", explicit=False)

    # Strip think blocks and any fence wrapping the *whole* response first.
    # Without this, a model that fences its entire answer would put "```" on
    # line one, the action line would go unrecognised, and the fenced block --
    # action line and all -- would be handed to the database as SQL.
    text = extract_sql(response)
    if not text.strip():
        return Action(DEFAULT_ACTION, "", explicit=False)

    first_line, _, remainder = text.partition("\n")
    stripped = first_line.strip()

    if not stripped.upper().startswith(ACTION_PREFIX):
        return Action(DEFAULT_ACTION, extract_sql(text), explicit=False)

    # Everything after the marker on the first line. A name may be followed by
    # its argument on the same line -- `ACTION: sample_rows track` -- so the
    # first whitespace-delimited token is the name and any tail joins the
    # argument.
    after_marker = stripped[len(ACTION_PREFIX) :].strip()
    name, _, inline_argument = after_marker.partition(" ")

    argument = inline_argument.strip()
    if remainder.strip():
        argument = f"{argument}\n{remainder}".strip() if argument else remainder

    # Stripped again: the argument itself may be fenced, which is a different
    # fence from the one handled above.
    return Action(name.strip().lower(), extract_sql(argument), explicit=True)
