"""Iteration 6 T5 — `POST /ask`.

**This is the first test file that imports `api.main` at all.** Until Iteration
6 the API's only endpoint was verified by `curl` against the container and by
nothing in the suite, which is why `fastapi` had to be added to
`requirements-dev.txt`: a module that cannot be imported cannot be asserted
about, and AC4 is an assertion about this module.

The provider is faked throughout. These tests are about the transport — status
codes, payload shape, and the safety properties — not about whether the model
answers well, which is what `evals/` measures and what no unit test should
pretend to.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.testclient import TestClient

from api.agent.orchestrator import AgentResult, Step
from api.db.execution import (
    CATEGORY_CONNECTION_ERROR,
    CATEGORY_DATABASE_ERROR,
    ExecutionResult,
)
from api.agent.single_shot import CATEGORY_NO_SQL, CATEGORY_RATE_LIMITED
from api.http.errors import STATUS_ANSWERED, STATUS_UNAVAILABLE
from api.main import app

MAIN_SOURCE = pathlib.Path("api/main.py").read_text(encoding="utf-8")


@pytest.fixture
def client():
    return TestClient(app)


def _answer(monkeypatch, result: AgentResult) -> None:
    """Replace the agent with a fixed result.

    Patched where `api.main` looked it up, not where it is defined -- patching
    `api.agent.orchestrator.answer` would leave the name `api.main` already
    imported pointing at the real function.
    """
    # `**_` absorbs `provider=`, which T3 added so the endpoint can time
    # `complete()` separately (AC2). A double that pins the old signature would
    # fail for a reason that has nothing to do with what its test asserts.
    monkeypatch.setattr("api.main.answer", lambda question, **_: result)


def _ok(columns, rows, sql="SELECT 1", steps=()) -> AgentResult:
    return AgentResult(
        ok=True,
        question="q",
        sql=sql,
        result=ExecutionResult(ok=True, columns=tuple(columns), rows=tuple(rows)),
        steps=tuple(steps),
        attempts_used=1,
    )


# --- AC4 and AC5: the structural properties ---------------------------------


def _function_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(MAIN_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in api/main.py")


def test_ac4_no_endpoint_touches_the_database_directly():
    """Charter §4: no code path executes SQL without passing Gate 2.

    **Scoped to the whole module, which it could not be at T5.** `/health` had
    run its own probe through `get_engine()` and `conn.execute(text(...))` since
    Iteration 0 — a genuine, undocumented exception to a rule the charter states
    absolutely. Narrowing this test to `ask()` would have quietly institution-
    alised it. `/health` now goes through `execute_sql()` instead, so the strong
    assertion is available and is what is made here.

    Asserted against the parsed AST, never by grepping the file: this project
    has twice written a structural test that matched its own docstring.
    """
    forbidden = {"get_engine", "text", "connect", "engine"}
    called = set()
    for node in ast.walk(ast.parse(MAIN_SOURCE)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    leaked = forbidden & called
    assert not leaked, f"api/main.py reaches the database directly via {sorted(leaked)}"


def test_ac4_the_only_database_call_in_the_module_is_execute_sql():
    """The positive half, and the reason the negative half is meaningful.

    `execute_sql()` is the single entry point, so both endpoints inherit Gate 2,
    the read-only transaction and the statement timeout without either handler
    having to remember them.
    """
    tree = ast.parse(MAIN_SOURCE)
    imported_from_db = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith(("api.db", "sqlalchemy"))
        for alias in node.names
    }
    assert imported_from_db <= {"execute_sql"}, (
        f"api/main.py imports database internals: {sorted(imported_from_db)}"
    )


def _calls_in(name: str) -> set[str]:
    """Plain function calls made inside one function of `api/main.py`."""
    return {
        node.func.id
        for node in ast.walk(_function_node(name))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_ac4_the_endpoint_reaches_the_database_only_through_the_agent():
    """The positive half: the endpoint's only route to the database is
    `answer()`, which is the only route to `execute_sql()` and therefore to
    Gate 2.

    **Followed through one level of indirection since Iteration 7 T4.** The
    cache sits between the endpoint and the agent — that is the entire point of
    it, since an answer already paid for must not be bought twice — so `ask()`
    calls `_answer_or_replay()`, which calls `answer()`. Asserting that `ask()`
    itself names `answer` would now fail for a reason that has nothing to do
    with the property being protected. Asserting the chain keeps the property
    and still fails closed: insert another hop and this test goes red until
    somebody looks at it.
    """
    assert "_answer_or_replay" in _calls_in("ask")
    assert "answer" in _calls_in("_answer_or_replay")


def test_ac5_the_deployed_api_does_not_pace():
    """B-1: pacing belongs to the benchmark only.

    Sleeping inside a user's request trades a rare refusal for a guaranteed
    delay on every question. Asserted over the whole module's imports, because
    a pacing wrapper installed anywhere in `api/main.py` would apply to the
    endpoint regardless of which function mentions it.
    """
    tree = ast.parse(MAIN_SOURCE)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("pacing" in name for name in imported)
    assert "PacedProvider" not in imported


# --- the contract -----------------------------------------------------------


def test_a_scalar_answer_carries_its_shape_and_sql(client, monkeypatch):
    _answer(monkeypatch, _ok(["count"], [[3503]], sql="SELECT count(*) FROM track"))
    response = client.post("/ask", json={"question": "how many tracks?"})

    assert response.status_code == STATUS_ANSWERED
    body = response.json()
    assert body["ok"] is True
    assert body["shape"] == "scalar"
    assert body["sql"] == "SELECT count(*) FROM track"
    assert body["rows"] == [[3503]]
    assert body["error"] == ""


def test_decimals_reach_the_client_as_exact_strings(client, monkeypatch):
    """D-1 end to end. The float round trip renders this `2328.6`."""
    import decimal

    _answer(monkeypatch, _ok(["sum"], [[decimal.Decimal("2328.60")]]))
    body = client.post("/ask", json={"question": "total?"}).json()
    assert body["rows"] == [["2328.60"]]


def test_a_chartable_result_is_labelled_not_charted(client, monkeypatch):
    """Q-B: the server says a chart is *offerable*; the human decides."""
    _answer(monkeypatch, _ok(["genre", "n"], [["Rock", 1297], ["Jazz", 130]]))
    body = client.post("/ask", json={"question": "genres?"}).json()
    assert body["shape"] == "chartable"


def test_an_empty_result_is_not_a_failure(client, monkeypatch):
    """Zero rows answers the question; `ok` stays true."""
    _answer(monkeypatch, _ok(["name"], []))
    response = client.post("/ask", json={"question": "customers in Antarctica?"})
    assert response.status_code == STATUS_ANSWERED
    body = response.json()
    assert body["ok"] is True
    assert body["shape"] == "empty"


def test_the_trace_shows_the_retry_and_the_error_that_caused_it(client, monkeypatch):
    """Q-E, and charter §1's diagram: *read the error, revise*.

    A trace that showed the retry without the error it reacted to would omit
    the half that makes this an agent.
    """
    steps = (
        Step(
            attempt=1,
            action="execute_sql",
            sql="SELECT artist_name FROM track",
            ok=False,
            category=CATEGORY_DATABASE_ERROR,
            error='column "artist_name" does not exist',
        ),
        Step(attempt=2, action="execute_sql", sql="SELECT name FROM artist", ok=True),
    )
    _answer(monkeypatch, _ok(["name"], [["AC/DC"], ["Aerosmith"]], steps=steps))
    body = client.post("/ask", json={"question": "artists?"}).json()

    assert [entry["attempt"] for entry in body["trace"]] == [1, 2]
    assert body["trace"][0]["ok"] is False
    assert "artist_name" in body["trace"][0]["error"]
    assert body["trace"][1]["ok"] is True


# --- D-2: which failures are HTTP errors ------------------------------------


def _failed(category: str) -> AgentResult:
    return AgentResult(ok=False, question="q", category=category)


def test_a_question_the_agent_could_not_answer_returns_200(client, monkeypatch):
    """D-2. The request was understood and processed; the answer is negative."""
    _answer(monkeypatch, _failed(CATEGORY_NO_SQL))
    response = client.post("/ask", json={"question": "?"})
    assert response.status_code == STATUS_ANSWERED
    body = response.json()
    assert body["ok"] is False
    assert body["category"] == CATEGORY_NO_SQL
    assert body["error"]


def test_an_unreachable_database_is_a_service_error(client, monkeypatch):
    _answer(monkeypatch, _failed(CATEGORY_CONNECTION_ERROR))
    response = client.post("/ask", json={"question": "?"})
    assert response.status_code == STATUS_UNAVAILABLE


def test_a_rate_limit_is_a_service_error_and_says_so_plainly(client, monkeypatch):
    """B-1: a billing condition must never arrive wearing a security message."""
    _answer(monkeypatch, _failed(CATEGORY_RATE_LIMITED))
    response = client.post("/ask", json={"question": "?"})
    assert response.status_code == STATUS_UNAVAILABLE
    body = response.json()
    assert body["retryable"] is True
    assert "reject" not in body["error"].lower()
    assert "limit" in body["error"].lower()


def test_a_failure_still_carries_the_sql_that_failed(client, monkeypatch):
    """AC12: a demo audience learns more from a legible failure than a spinner
    that stops."""
    result = AgentResult(
        ok=False,
        question="q",
        sql="SELECT nope FROM track",
        category=CATEGORY_DATABASE_ERROR,
    )
    _answer(monkeypatch, result)
    body = client.post("/ask", json={"question": "?"}).json()
    assert body["sql"] == "SELECT nope FROM track"


def test_no_failure_message_leaks_internals_to_the_caller(client, monkeypatch):
    _answer(monkeypatch, _failed(CATEGORY_DATABASE_ERROR))
    body = client.post("/ask", json={"question": "?"}).json()
    for leak in ("sqlstate", "traceback", "groq"):
        assert leak not in body["error"].lower()


# --- request validation -----------------------------------------------------


def test_an_empty_question_is_refused(client):
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_a_missing_question_is_refused(client):
    assert client.post("/ask", json={}).status_code == 422


def test_a_question_containing_sql_is_not_sanitised(client, monkeypatch):
    """`007` AC20: the question reaches the agent verbatim.

    Stripping or escaping would corrupt legitimate English -- apostrophes and
    the word "select" both occur naturally -- and would be the wrong defence
    regardless, since Gate 2 validates whatever the model writes.
    """
    seen: list[str] = []

    def capture(question: str, **_) -> AgentResult:
        seen.append(question)
        return _ok(["count"], [[1]])

    monkeypatch.setattr("api.main.answer", capture)
    hostile = "'; DROP TABLE track; -- how many tracks?"
    client.post("/ask", json={"question": hostile})
    assert seen == [hostile]


# --- T6: the page ------------------------------------------------------------


def test_the_page_is_served_at_the_root(client):
    """AC7: a question can be asked in a browser with no terminal."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "QueryPilot" in response.text


def test_the_static_assets_are_reachable(client):
    for path, fragment in (
        ("/static/app.js", "renderScalar"),
        ("/static/styles.css", ".scalar-value"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert fragment in response.text, path


def test_the_page_references_the_assets_it_is_served_with(client):
    """Catches the rename that leaves a page loading a stylesheet that 404s --
    a failure a human notices only by the page looking wrong."""
    page = client.get("/").text
    assert "/static/app.js" in page
    assert "/static/styles.css" in page


def test_the_web_directory_resolves_from_the_module_not_the_cwd(tmp_path, monkeypatch):
    """The container lands this package at /app/api/ while pytest runs from the
    repository root, so a cwd-relative path works in exactly one of them."""
    import os

    from api.main import _WEB_DIR

    monkeypatch.chdir(tmp_path)
    assert _WEB_DIR.is_absolute()
    assert (_WEB_DIR / "index.html").exists()
    assert os.getcwd() != str(_WEB_DIR.parent.parent)


def test_no_accuracy_claim_appears_in_the_interface(client):
    """AC13. The held-out figure is one pass at 100.0% and honestly reads
    *between 90% and 100%*; a product surface is where that nuance dies.

    **Comments are stripped first, and the first version of this test failed
    because they were not.** The HTML carries a comment explaining AC13, and
    that explanation necessarily contains the phrases AC13 forbids -- "100.0%",
    "accuracy". This is `HANDOFF.md` §6's *a structural test that greps source
    will match its own docstring*, recurring in a third costume: not a Python
    docstring this time but an HTML comment.

    The fix is the one that trap always wants: assert about the thing that
    actually reaches the reader. A comment is not rendered, so it is not a
    claim.
    """
    import re

    rendered = re.sub(r"<!--.*?-->", "", client.get("/").text, flags=re.DOTALL).lower()
    for claim in ("100%", "100.0%", "accurate", "accuracy", "confidence", "always correct"):
        assert claim not in rendered, f"the page claims {claim!r}"


def test_the_accuracy_guard_is_not_vacuous():
    """Proves the comment-stripping above did not simply delete the whole page.

    A regex that over-matched would empty the document and make every
    assertion pass, which is the failure mode of every "assert not present"
    test.

    **Checks that the markup survived, not that the file stayed long.** The
    first version required the stripped page to be over half the raw file, and
    T5 broke it by adding two well-commented sections: the page became 55%
    commentary and the guard read that as an over-matching regex. Length was
    only ever a proxy, and a proxy that fails when someone explains their work
    is training to write less of it down. Naming the elements that must survive
    tests the actual property -- and it fails on a truly greedy regex, which
    would take the page down to nothing and every landmark with it.
    """
    import re

    from api.main import _WEB_DIR

    raw = _WEB_DIR.joinpath("index.html").read_text(encoding="utf-8")
    rendered = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)

    assert "QueryPilot" in rendered
    for landmark in (
        '<form id="ask-form"',
        'id="question"',
        'id="result"',
        'id="sql"',
        'id="quota"',
        'id="cache-note"',
        "<footer>",
        '<script src="/static/app.js">',
    ):
        assert landmark in rendered, f"stripping comments removed {landmark}"

    # ... and the raw file really does contain what the stripping removes,
    # so the test above is exercising the strip rather than passing by luck.
    assert "accuracy" in raw.lower()
    assert len(rendered) < len(raw), "nothing was stripped; the test proves nothing"
    assert "accuracy" not in rendered.lower()


def test_the_page_does_not_hide_the_sql_behind_a_toggle(client):
    """AC8. The SQL is the audit of the answer and is always visible; only the
    trace -- the audit of the *process* -- is collapsed (Q-E)."""
    page = client.get("/").text
    sql_index = page.index('id="sql"')
    details_index = page.index("<details")
    assert sql_index < details_index, "the SQL block must not be inside <details>"


# --- T7: the chart toggle ----------------------------------------------------


def test_a_chartable_result_says_which_column_the_bars_come_from(client, monkeypatch):
    """The client must not re-derive the measure.

    Deriving it separately is how a chart ends up plotting the label column:
    two rules that agree today and diverge on the first result neither author
    considered. `chart_series()` decides once, server-side.
    """
    _answer(monkeypatch, _ok(["genre", "n"], [["Rock", 1297], ["Jazz", 130]]))
    body = client.post("/ask", json={"question": "genres?"}).json()
    assert body["shape"] == "chartable"
    assert body["series"] == {"label": 0, "measure": 1}


def test_the_series_survives_the_columns_being_the_other_way_round(client, monkeypatch):
    _answer(monkeypatch, _ok(["n", "genre"], [[1297, "Rock"], [130, "Jazz"]]))
    body = client.post("/ask", json={"question": "genres?"}).json()
    assert body["series"] == {"label": 1, "measure": 0}


def test_a_non_chartable_result_offers_no_series(client, monkeypatch):
    """A scalar has nothing to chart, and the payload says so rather than
    leaving the client to infer it from `shape`."""
    _answer(monkeypatch, _ok(["count"], [[3503]]))
    body = client.post("/ask", json={"question": "how many?"}).json()
    assert body["shape"] == "scalar"
    assert body["series"] is None


def test_the_chart_starts_hidden_in_the_served_markup(client):
    """Resolved Q-B: the toggle defaults to **off**.

    Asserted against the page rather than against app.js, because the default
    is expressed declaratively in the markup. A default that lived only in a
    script would be a default no test could see without a browser, and it could
    drift from the `aria-expanded` the same script sets.
    """
    page = client.get("/").text

    controls = page[page.index('id="chart-controls"') : page.index('id="chart-toggle"')]
    assert "hidden" in controls, "the chart controls must not appear before a result"

    chart_div = page[page.index('id="chart"') :][:40]
    assert "hidden" in chart_div, "the chart itself must start hidden"

    toggle = page[page.index('id="chart-toggle"') : page.index("Show chart")]
    assert 'aria-expanded="false"' in toggle
    assert "Show chart" in page and "Hide chart" not in page


def test_the_chart_is_drawn_without_a_charting_library(client):
    """Resolved D-3, and Q-A's dependency-free property.

    A CDN script tag would make "the only setup instruction is
    `docker compose up`" false, and would put a demo at the mercy of a network
    it should not need.
    """
    page = client.get("/").text
    assert "<script" in page
    assert "cdn" not in page.lower()
    assert "https://" not in page, "the page must not load anything remote"

    app_js = client.get("/static/app.js").text
    assert "createElementNS" in app_js, "the chart is built as real SVG nodes"
    assert "import " not in app_js, "no module imports; this file is loaded as-is"


def _javascript_code(source: str) -> str:
    """`source` with its comments removed.

    Line comments are stripped only when they begin a line, because `//` also
    occurs inside string literals -- `"http://www.w3.org/2000/svg"` is in this
    very file, and a naive stripper would truncate the line that defines the SVG
    namespace and quietly weaken every assertion made against it.
    """
    import re

    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.strip().startswith("//")
    )


def test_the_chart_never_builds_markup_from_a_string(client):
    """Database values and model-written SQL are data, not markup.

    `innerHTML` anywhere in this file would be an injection route from a table
    the model chose into the page.

    **Comments are stripped first, and the first version of this test failed
    because they were not** -- app.js carries a comment *promising* never to
    assign to `innerHTML`, and the promise contains the word. That is
    `HANDOFF.md` §6's grep-matches-its-own-docstring trap for the fourth time in
    this iteration, after a Python docstring and an HTML comment. The rule
    generalises: any "this string must not appear" test in this repository has
    to look at code rather than commentary.
    """
    code = _javascript_code(client.get("/static/app.js").text)
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code


def test_the_comment_stripper_does_not_gut_the_file():
    """Proves the assertion above is not passing over an empty string.

    Also pins the specific hazard the stripper was written around: the SVG
    namespace URL survives, so a line comment inside a string cannot silently
    delete real code.
    """
    from api.main import _WEB_DIR

    raw = _WEB_DIR.joinpath("app.js").read_text(encoding="utf-8")
    code = _javascript_code(raw)

    assert "innerHTML" in raw, "the file should still explain the rule in a comment"
    assert "innerHTML" not in code, "...and not use it in code"
    assert "http://www.w3.org/2000/svg" in code, "the SVG namespace must survive"
    assert "createElementNS" in code
    assert len(code) > len(raw) * 0.4
