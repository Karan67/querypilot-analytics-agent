"""Iteration 7 T7 — the history reader (AC4).

Everything the store has recorded since T3 was, until this page, readable only
by opening SQLite by hand inside a container. AC4 asks for it to be *readable*,
and the plan's acceptance for this task is that a human reads it.

Two properties get the most attention here, because both are ways a reader can
be quietly wrong rather than visibly broken.

**An unreadable store must not render as an empty one.** `recent()` raising and
`recent()` returning nothing are different facts, and the second one reads as
*nobody has asked anything*, which is a lie about the data rather than a gap in
it.

**A cached answer cost zero tokens for that request and was not free.** It was
paid for once, by the answer that produced it. A reader showing a bare `0`
alongside a real cost invites exactly the wrong conclusion about what the
system spends.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.llm.base import TokenUsage
from api.main import app
from api.store import history
from api.store.history import AskRecord, record_ask


@pytest.fixture
def client():
    return TestClient(app)


def _record(**overrides) -> str:
    base = dict(
        question="How many tracks are in the library?",
        ok=True,
        total_ms=812,
        provider_ms=653,
        sql="SELECT count(*) FROM track",
        category="",
        shape="scalar",
        row_count=1,
        attempts_used=1,
        total_tokens=1069,
        prompt_tokens=1000,
        usage_measured=True,
        provider_calls=1,
        model="openai/gpt-oss-120b",
        schema_fp="c0418e1ed384",
        prompt_fp="91036a089282",
    )
    base.update(overrides)
    return record_ask(AskRecord(**base))


# --- the data ---------------------------------------------------------------


def test_a_recorded_answer_is_readable(client):
    ask_id = _record()
    body = client.get("/history/data").json()

    assert body["ok"] is True
    assert len(body["answers"]) == 1

    answer = body["answers"][0]
    assert answer["id"] == ask_id
    assert answer["question"] == "How many tracks are in the library?"
    assert answer["sql"] == "SELECT count(*) FROM track"
    assert answer["tokens"] == 1069
    assert answer["measured"] is True
    assert answer["total_ms"] == 812 and answer["provider_ms"] == 653


def test_the_trace_comes_back_with_the_answer(client):
    """Charter §1's claim is *read the error, revise*. A reader that shows only
    final answers cannot show that ever happened, which is most of what makes
    this an agent rather than a wrapper around one prompt."""
    ask_id = _record(
        attempts_used=2,
        steps=(
            {
                "attempt": 1,
                "action": "execute_sql",
                "ok": False,
                "category": "database_error",
                "error": 'column "artist_name" does not exist',
                "sql": "SELECT artist_name FROM track",
            },
            {"attempt": 2, "action": "execute_sql", "ok": True, "sql": "SELECT name FROM artist"},
        ),
    )

    answer = next(a for a in client.get("/history/data").json()["answers"] if a["id"] == ask_id)

    assert [s["attempt"] for s in answer["trace"]] == [1, 2]
    assert answer["trace"][0]["ok"] is False
    assert "artist_name" in answer["trace"][0]["error"]
    assert answer["trace"][1]["ok"] is True


def test_a_failed_answer_is_readable_too(client):
    """A history that shows only successes cannot answer *what does it get
    wrong*, which is most of what AC1 exists for."""
    _record(ok=False, category="no_sql_returned", sql="", shape="", row_count=None)

    answer = client.get("/history/data").json()["answers"][0]
    assert answer["ok"] is False
    assert answer["category"] == "no_sql_returned"


def test_the_row_says_which_instrument_measured_the_cost(client):
    """D-1's standing rule, carried to the last reader in the chain. A billed
    figure and a locally counted one are different quantities, and a reader that
    shows them identically invites somebody to add them together."""
    _record(question="billed", total_tokens=1069, usage_measured=True)
    _record(question="estimated", total_tokens=1100, usage_measured=False)

    by_question = {a["question"]: a for a in client.get("/history/data").json()["answers"]}
    assert by_question["billed"]["measured"] is True
    assert by_question["estimated"]["measured"] is False


def test_the_row_carries_what_produced_it(client):
    """Without the fingerprints a row stops being interpretable the moment the
    prompt or the schema moves -- the same reason an `EVALS.md` entry carries
    them, applied to a log rather than to a benchmark."""
    _record()
    answer = client.get("/history/data").json()["answers"][0]

    assert answer["model"] == "openai/gpt-oss-120b"
    assert answer["prompt_fp"] == "91036a089282"
    assert answer["schema_fp"] == "c0418e1ed384"


def test_newest_first(client):
    for i in range(3):
        _record(question=f"question {i}")
    questions = [a["question"] for a in client.get("/history/data").json()["answers"]]
    assert questions[0] == "question 2"


# --- an unreadable store is not an empty one --------------------------------


def test_an_unreadable_store_is_not_reported_as_empty(client, monkeypatch):
    """**The failure this reader could most easily get wrong.**

    `recent()` raising and `recent()` returning nothing are different facts, and
    rendering the first as the second says *nobody has ever asked anything* --
    a claim about the data rather than an admission about the store.

    503 rather than 200, because unlike `record_ask` a reader **is** allowed to
    fail: someone opening this page asked whether the history is readable, and
    no is the true answer.
    """
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: ask")

    monkeypatch.setattr(history, "_connect", explode)

    response = client.get("/history/data")
    assert response.status_code == 503

    body = response.json()
    assert body["ok"] is False
    assert body["answers"] == []
    assert "no such table" in body["error"]


def test_an_empty_store_is_reported_as_empty(client):
    """The other half: genuinely nothing recorded is a 200 with no answers, so
    the two states stay distinguishable."""
    response = client.get("/history/data")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": "", "answers": []}


# --- the limit --------------------------------------------------------------


def test_the_limit_is_capped(client):
    """The endpoint takes its limit from the query string, so an uncapped one is
    a way to ask this process to build an arbitrarily large document."""
    from api.main import HISTORY_MAX_LIMIT

    for i in range(5):
        _record(question=f"q{i}")

    assert len(client.get("/history/data?limit=2").json()["answers"]) == 2
    assert client.get(f"/history/data?limit={HISTORY_MAX_LIMIT * 100}").status_code == 200
    assert len(client.get("/history/data?limit=0").json()["answers"]) >= 1


# --- the trace query --------------------------------------------------------


def test_traces_are_fetched_in_one_query_not_one_per_answer():
    """**The N+1 this reader would otherwise be.**

    `_connect` opens a connection per operation by design, so calling
    `steps_for` in a loop would be fifty-one connections to render fifty rows --
    and every one of them contends for the same lock the answers are being
    recorded through.

    Counted by patching the connection factory, because the cost is invisible in
    the output: both shapes return identical data.
    """
    ids = [_record(question=f"q{i}", steps=({"attempt": 1, "action": "execute_sql", "ok": True},))
           for i in range(6)]

    real_connect = history._connect
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_connect(*args, **kwargs)

    history._connect = counting
    try:
        grouped = history.steps_for_ids(ids)
    finally:
        history._connect = real_connect

    assert calls["n"] == 1, f"one query for six traces, not {calls['n']}"
    assert set(grouped) == set(ids)
    assert all(len(steps) == 1 for steps in grouped.values())


def test_steps_for_ids_handles_an_empty_list():
    """An empty `IN ()` is a syntax error in SQLite, and a page with no answers
    is the first thing anybody sees."""
    assert history.steps_for_ids([]) == {}


def test_steps_for_ids_does_not_interpolate_ids_into_sql():
    """These ids are uuids this process generated, so nothing hostile reaches
    them today. A query assembled by string formatting is still a habit that
    outlives the context that made it safe.
    """
    import ast
    import inspect

    source = inspect.getsource(history.steps_for_ids)
    tree = ast.parse(source.strip())

    # The only f-string in the function is the placeholder list, which contains
    # no id values -- the ids go through parameters.
    joined = [
        node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
    ]
    assert len(joined) == 1
    rendered = "".join(
        part.value for part in joined[0].values if isinstance(part, ast.Constant)
    )
    assert "?" not in rendered or "IN (" in rendered
    assert "ask_id IN" in rendered


# --- the page ---------------------------------------------------------------


def _javascript_code(source: str) -> str:
    """`source` with comments removed; line comments only where they begin a
    line, because `//` also appears inside string literals in this project."""
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.strip().startswith("//")
    )


def test_the_page_is_served(client):
    page = client.get("/history").text
    assert "History" in page
    assert '<script src="/static/history.js">' in page


def test_the_reader_never_builds_markup_from_a_string(client):
    """**Sharper here than on the answer page.** Every question in this list is
    text a user typed and every SQL string was written by a language model, and
    both are replayed into the DOM. `innerHTML` anywhere in this file would turn
    the history log into a stored-XSS surface.

    Comments are stripped first — the repository rule, after that trap appeared
    in four costumes during Iteration 6.
    """
    code = _javascript_code(client.get("/static/history.js").text)

    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code
    assert "textContent" in code, "the stripper gutted the file; the test proves nothing"


def test_the_comment_stripper_did_not_gut_the_reader():
    """Pairs with the assertion above, because an over-matching regex makes
    every absence assertion pass."""
    from api.main import _WEB_DIR

    raw = _WEB_DIR.joinpath("history.js").read_text(encoding="utf-8")
    code = _javascript_code(raw)

    assert "innerHTML" in raw, "the file should still explain the rule in a comment"
    assert "innerHTML" not in code, "...and not use it in code"
    assert "createElement" in code
    assert len(code) > len(raw) * 0.3


def test_the_reader_makes_no_accuracy_claim(client):
    """AC13 applies to this page too, and it is *more* tempting here: the reader
    knows how many answers succeeded, and that number looks exactly like an
    accuracy rate. It is not one -- it describes whatever was typed into the box,
    with no gold answers and no held-out split.

    Comments are stripped before asserting, because the footer's explanation of
    this rule necessarily contains the words the rule forbids.

    Whitespace is collapsed before asserting. The first version matched the raw
    text and broke when the disclaimer was rewrapped across two lines -- an
    assertion about where a paragraph happens to fold rather than about what it
    says.
    """
    stripped = re.sub(r"<!--.*?-->", "", client.get("/history").text, flags=re.DOTALL)
    rendered = " ".join(stripped.split()).lower()

    for claim in ("accuracy", "accurate", "% correct", "success rate", "confidence"):
        assert claim not in rendered, f"the history page claims {claim!r}"

    assert "not a benchmark" in rendered, "and it says so explicitly"


def test_the_page_loads_nothing_remote(client):
    page = client.get("/history").text
    assert "https://" not in page
    assert "cdn" not in page.lower()


def test_a_cached_answer_is_not_shown_as_free(client):
    """A hit spent nothing on *this* request and was paid for once, by the answer
    that produced it. A bare `0` beside a real cost invites the wrong conclusion
    about what the system spends, so the reader labels it."""
    code = _javascript_code(client.get("/static/history.js").text)

    assert "cache_hit" in code
    assert "reused" in code, "a zero must be explained, not merely printed"


def test_the_summary_totals_need_no_cache_filter(client):
    """The reader sums the token column with no `cache_hit` filter, and that is
    correct **only because** T4 decided a hit records zero rather than replaying
    the original figures. This test pins the two decisions together, so changing
    one without the other fails here rather than in a spend report.
    """
    _record(question="paid", total_tokens=1069, provider_calls=1, cache_hit=False)
    _record(question="hit", total_tokens=0, provider_calls=0, cache_hit=True)
    _record(question="hit again", total_tokens=0, provider_calls=0, cache_hit=True)

    answers = client.get("/history/data").json()["answers"]

    assert sum(a["tokens"] for a in answers) == 1069, "the plain sum must be the billed total"
    assert sum(a["provider_calls"] for a in answers) == 1
    assert sum(1 for a in answers if a["cache_hit"]) == 2


def test_the_reader_does_not_record_anything(client):
    """Reading history must not write history, or the log becomes a record of
    people looking at it."""
    _record()
    before = len(history.recent())

    client.get("/history/data")
    client.get("/history")

    assert len(history.recent()) == before


def test_the_page_scripts_actually_parse():
    """**A gap noticed at T7, covering both pages.**

    Every other assertion about this JavaScript reads it as *text*: no
    `innerHTML`, no CDN, the right names present. All of those pass on a file
    with a syntax error in it, and such a file is served with a 200, renders a
    blank page, and reports the problem only to a browser console nobody is
    watching. That is the worst shape of failure this project keeps finding --
    green tests over something that does not work.

    Skipped rather than failed where Node is unavailable, the same way the suite
    skips when the database is down: a missing tool is an environment problem,
    and reporting it as a defect buries the line that says what to install.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH; cannot syntax-check the page scripts")

    from api.main import _WEB_DIR

    for name in ("app.js", "history.js"):
        result = subprocess.run(
            [node, "--check", str(_WEB_DIR / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name} does not parse:\n{result.stderr}"
