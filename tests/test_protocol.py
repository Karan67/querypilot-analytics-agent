"""Action protocol tests — `specs/007-agent-loop.md` AC14, AC19.

**Pure.** No database, no provider, no API key.

The rule under test throughout is that the *first line* names the action and
everything after it is opaque. Every other design would let a value inside the
argument decide what runs.
"""

from __future__ import annotations

import pytest

from api.agent.protocol import ACTION_PREFIX, DEFAULT_ACTION, Action, parse_action


# --- the happy path ---------------------------------------------------------


def test_a_well_formed_action_parses():
    action = parse_action("ACTION: execute_sql\nSELECT count(*) FROM track")
    assert action == Action("execute_sql", "SELECT count(*) FROM track", explicit=True)


def test_an_action_with_no_argument_parses():
    action = parse_action("ACTION: get_schema")
    assert action.name == "get_schema"
    assert action.argument == ""
    assert action.explicit is True


def test_an_argument_on_the_same_line_parses():
    """`ACTION: sample_rows track` is a plausible shape and costs one line to
    support. The probe never exercised `sample_rows`, so this is the one action
    whose formatting is unmeasured — which is a reason to be permissive, not a
    reason to assume."""
    action = parse_action("ACTION: sample_rows track")
    assert action.name == "sample_rows"
    assert action.argument == "track"


def test_a_multi_line_argument_keeps_its_newlines():
    sql = "SELECT a,\n       b\nFROM t"
    assert parse_action(f"ACTION: execute_sql\n{sql}").argument == sql


# --- the rule that matters --------------------------------------------------


def test_action_text_inside_the_sql_is_not_a_marker():
    """**The reason the first line is the only line that counts.**

    SQL is free text. A query selecting a literal containing `ACTION:` must
    reach the database intact, and must not be able to redirect dispatch. A
    parser scanning for markers anywhere would be ambiguous by construction, and
    ambiguous in the direction where data decides what runs.
    """
    sql = "SELECT 'ACTION: get_schema' AS note FROM track"
    action = parse_action(f"ACTION: execute_sql\n{sql}")

    assert action.name == "execute_sql"
    assert action.argument == sql, "the argument must survive byte-for-byte"


def test_a_later_action_line_is_part_of_the_argument():
    action = parse_action("ACTION: execute_sql\nSELECT 1\nACTION: get_schema")
    assert action.name == "execute_sql"
    assert "ACTION: get_schema" in action.argument


def test_only_the_first_line_is_inspected_even_when_it_is_not_an_action():
    """The mirror case: an `ACTION:` line further down does not promote a bare
    response into an explicit one."""
    action = parse_action("SELECT 1\nACTION: get_schema")
    assert action.explicit is False
    assert action.name == DEFAULT_ACTION


# --- plan §2.4: bare output is execute_sql ---------------------------------


def test_bare_sql_becomes_execute_sql():
    """Compliance measured 24/24, but `005` AC3 makes the model an environment
    variable and `005` measured this same model emitting bare SQL under the
    single-shot prompt. A non-complying model degrades to single-shot behaviour
    rather than failing every turn on a parse error."""
    action = parse_action("SELECT count(*) FROM track")
    assert action == Action(DEFAULT_ACTION, "SELECT count(*) FROM track", explicit=False)


def test_bare_prose_becomes_execute_sql_and_is_marked_inexplicit():
    """A refusal takes this path, and `explicit=False` is how the loop later
    tells it apart from a genuine SQL mistake — without reading the content."""
    action = parse_action("I'm sorry, but I can't help with that.")
    assert action.name == DEFAULT_ACTION
    assert action.explicit is False


@pytest.mark.parametrize("response", ["", "   ", "\n\n", None, 42, [], object()])
def test_empty_and_non_string_input_is_survivable(response):
    """Never raises, on any input. A parser that threw would end a run over a
    formatting slip."""
    action = parse_action(response)
    assert action.name == DEFAULT_ACTION
    assert action.argument == ""
    assert action.explicit is False


# --- tolerance --------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "ACTION: execute_sql",
        "action: execute_sql",
        "Action: execute_sql",
        "  ACTION:   execute_sql  ",
        "ACTION:execute_sql",
    ],
)
def test_marker_and_name_are_case_and_whitespace_tolerant(line):
    action = parse_action(f"{line}\nSELECT 1")
    assert action.name == "execute_sql"
    assert action.explicit is True
    assert action.argument == "SELECT 1"


def test_the_name_is_lowercased():
    assert parse_action("ACTION: EXECUTE_SQL\nSELECT 1").name == "execute_sql"


def test_a_fenced_whole_response_is_unwrapped_before_the_action_line_is_read():
    """Without this the fence would occupy line one, the action line would go
    unrecognised, and the fenced block — action line included — would be handed
    to the database as SQL."""
    action = parse_action("```\nACTION: execute_sql\nSELECT 1\n```")
    assert action.name == "execute_sql"
    assert action.argument == "SELECT 1"
    assert action.explicit is True


def test_a_fenced_argument_is_unwrapped():
    action = parse_action("ACTION: execute_sql\n```sql\nSELECT 1\n```")
    assert action.argument == "SELECT 1"


def test_an_inline_reasoning_block_is_stripped():
    """`005` measured a model that inlines `<think>` into content and fails
    Gate 2 on every question because of it."""
    action = parse_action("<think>hmm</think>\nACTION: execute_sql\nSELECT 1")
    assert action.name == "execute_sql"
    assert action.argument == "SELECT 1"


# --- AC19: parsing is dispatch, not a safety decision -----------------------


def test_an_unknown_action_name_is_returned_not_resolved():
    """The registry is the allow-list. This module validates nothing, so there
    is exactly one place that decides what is callable rather than two that both
    have to be right."""
    assert parse_action("ACTION: lookup_table\ntrack").name == "lookup_table"


@pytest.mark.parametrize(
    "name",
    ["__import__", "os.system", "eval", "TOOLS", "_dispatch", "../../etc/passwd"],
)
def test_a_hostile_action_name_is_just_an_unknown_name(name):
    """Nothing is resolved dynamically — no `getattr`, no import by name — so a
    hostile action name is inert text that will fail a dictionary lookup."""
    action = parse_action(f"ACTION: {name}\nwhatever")
    assert action.name == name.split(" ")[0].lower()
    assert action.explicit is True


def test_the_module_resolves_nothing_dynamically():
    """Asserted against the parsed module, not its text — a source grep matches
    its own docstring, which has caught this project out twice."""
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("api/agent/protocol.py").read_text(encoding="utf-8")
    )
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for forbidden in ("getattr", "eval", "exec", "__import__", "globals", "vars"):
        assert forbidden not in called, f"protocol.py calls {forbidden}()"


def test_the_prefix_is_a_named_constant():
    assert ACTION_PREFIX == "ACTION:"


# --- structural -------------------------------------------------------------


def test_actions_are_frozen():
    with pytest.raises(Exception):
        parse_action("ACTION: get_schema").name = "execute_sql"  # type: ignore[misc]


def test_parsing_is_deterministic():
    response = "ACTION: execute_sql\nSELECT 1"
    assert parse_action(response) == parse_action(response)
