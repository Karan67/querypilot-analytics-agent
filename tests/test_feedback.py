"""Iteration 8 T6 — feedback: collect it, and do not consume it (AC13, AC14).

This closes AC6 of `010`, which Iteration 7 T1 deferred openly rather than
carrying unmet. `011-ship.md` §2.6 is why it is shaped the way it is:

```
answers recorded      : 29
  failed (ok = 0)     : 0
  needed a retry      : 0
```

**Zero wrong answers and zero retries.** The signal feedback exists to capture
is which answers were bad, and there were none in the record to mark — so this
task builds the collector and deliberately not the loop that would learn from
it. Q-E's ruling: *"cleanly persisting the signal"*.

Two properties get the most attention, and the second one is the reason this
file is long.

**A mark attaches to an answer, not to a question.** `ask.id` was made a uuid
returned in the `/ask` payload at Iteration 7 T3 for exactly this. The same
question asked twice is two answers, one of which may have been wrong, and a
mark keyed on the text would belong to neither.

**Nothing aggregates the marks (AC14).** Not the store, not the endpoint, not
the reader, not the page. A count is one line from any of them, and §2.6 means
any rate computed today reads as *100% good* over a sample containing no
failures — which is the accuracy claim `009` AC13 kept off the answer page,
arriving through a side door. Several tests here exist only to make adding one
fail.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.store import history
from api.store.history import AskRecord, UnknownAsk, record_ask, record_feedback

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "api" / "web"


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
        shape="scalar",
        row_count=1,
        attempts_used=1,
        total_tokens=1069,
        usage_measured=True,
        provider_calls=1,
        model="openai/gpt-oss-120b",
    )
    base.update(overrides)
    return record_ask(AskRecord(**base))


# --- the store --------------------------------------------------------------


def test_a_mark_is_stored_against_the_answer_id():
    ask_id = _record()

    feedback_id = record_feedback(ask_id, 1, "spot on")

    with history._connect() as conn:
        rows = list(conn.execute("SELECT * FROM feedback"))

    assert len(rows) == 1
    assert rows[0]["id"] == feedback_id
    assert rows[0]["ask_id"] == ask_id
    assert rows[0]["rating"] == 1
    assert rows[0]["note"] == "spot on"
    assert rows[0]["created_at"].endswith("+00:00"), "timestamps are UTC and explicit"


def test_the_note_is_optional():
    ask_id = _record()
    record_feedback(ask_id, -1)

    with history._connect() as conn:
        assert list(conn.execute("SELECT note FROM feedback"))[0]["note"] == ""


def test_an_unknown_answer_id_is_refused_by_the_store():
    """`UnknownAsk`, not a generic error, because the endpoint owes a 404.

    SQLite does not enforce the `REFERENCES ask (id)` clause without
    `PRAGMA foreign_keys=ON`, which this store does not issue — so without the
    explicit check the insert would *succeed* and leave a row nothing can ever
    interpret.
    """
    with pytest.raises(UnknownAsk):
        record_feedback("no-such-answer", 1)

    with history._connect() as conn:
        assert list(conn.execute("SELECT count(*) AS n FROM feedback"))[0]["n"] == 0


def test_marks_are_append_only_and_disagreement_survives():
    """Several marks on one answer, and none replaces another.

    Append-only for the reason `EVALS.md` is. A later mark overwriting an
    earlier one would silently destroy the disagreement — and on a record §2.6
    measured as containing zero bad answers, two people disagreeing about one
    answer is the most informative thing this table could hold.
    """
    ask_id = _record()

    record_feedback(ask_id, 1, "useful")
    record_feedback(ask_id, -1, "wrong on reflection")

    with history._connect() as conn:
        rows = list(conn.execute("SELECT rating, note FROM feedback ORDER BY rowid"))

    assert [row["rating"] for row in rows] == [1, -1]
    assert [row["note"] for row in rows] == ["useful", "wrong on reflection"]


def test_a_store_failure_is_raised_rather_than_swallowed(monkeypatch):
    """**The opposite policy to `record_ask`, and the opposite is correct.**

    `record_ask` swallows everything: a person who asked a question and got an
    answer must not be punished because our logging broke. Here the write *is*
    the request, so a silent failure would report success on a mark that was
    never stored — the same lie `/history/data` refuses when it returns 503
    instead of an empty list.
    """
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    real_connect = history._connect
    monkeypatch.setattr(history, "_connect", explode)

    with pytest.raises(sqlite3.OperationalError):
        record_feedback("any-id", 1)

    monkeypatch.setattr(history, "_connect", real_connect)


def test_feedback_for_ids_is_one_query_not_one_per_answer():
    """The N+1 `steps_for_ids` avoids, avoided again.

    The reader shows fifty answers; a call per row would be fifty-one
    connections to render one page, each competing for the write lock answers
    are recorded through.
    """
    ids = [_record(question=f"question {n}") for n in range(4)]
    for ask_id in ids:
        record_feedback(ask_id, 1)

    counted = {"n": 0}
    real_connect = history._connect

    import contextlib

    @contextlib.contextmanager
    def counting(path=None):
        counted["n"] += 1
        with real_connect(path) as conn:
            yield conn

    original = history._connect
    history._connect = counting
    try:
        grouped = history.feedback_for_ids(ids)
    finally:
        history._connect = original

    assert counted["n"] == 1, f"{counted['n']} connections for 4 answers"
    assert set(grouped) == set(ids)


def test_feedback_for_ids_returns_the_marks_and_not_a_summary():
    """AC14 at the store boundary.

    The store hands back the marks themselves. If it returned a count or a
    score, every caller would inherit the aggregate whether it wanted one or
    not, and AC14 would depend on each of them declining to show it.
    """
    ask_id = _record()
    record_feedback(ask_id, 1)
    record_feedback(ask_id, -1)

    grouped = history.feedback_for_ids([ask_id])

    assert isinstance(grouped[ask_id], list)
    assert len(grouped[ask_id]) == 2
    assert [row["rating"] for row in grouped[ask_id]] == [1, -1]


def test_no_marks_is_an_absent_key_rather_than_a_zero():
    ask_id = _record()
    assert history.feedback_for_ids([ask_id]) == {}


# --- the endpoint -----------------------------------------------------------


def test_ac13_posting_a_mark_returns_201_and_stores_it(client):
    ask_id = _record()

    response = client.post("/feedback", json={"id": ask_id, "rating": 1})

    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True
    assert body["id"] == ask_id
    assert body["rating"] == 1
    assert body["feedback_id"]

    with history._connect() as conn:
        assert list(conn.execute("SELECT ask_id FROM feedback"))[0]["ask_id"] == ask_id


def test_ac13_an_unknown_id_is_a_404(client):
    """Not a quiet accept, and not a 422.

    A `404` says the thing being marked does not exist, which is true and
    actionable. Accepting it would store a row nothing can interpret; a `422`
    would claim the request was malformed, when it was well-formed and pointed
    at nothing.
    """
    response = client.post("/feedback", json={"id": "not-an-answer", "rating": -1})

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert "not-an-answer" in body["error"]


@pytest.mark.parametrize("rating", [0, 2, -2, 5, 100, -1.5, "1", True, None])
def test_ac13_only_minus_one_and_one_are_accepted(client, rating):
    """Resolved D-6, enforced by the schema rather than by a hand-written check.

    `0` is in the list because it is the most likely thing a caller would send
    for "neutral", and accepting it would create a third value that averages to
    something. `True` is there because Python's `True == 1`, so a boolean is a
    real risk of arriving as a valid rating through coercion.
    """
    ask_id = _record()

    response = client.post("/feedback", json={"id": ask_id, "rating": rating})

    assert response.status_code == 422, f"{rating!r} was accepted"
    with history._connect() as conn:
        assert list(conn.execute("SELECT count(*) AS n FROM feedback"))[0]["n"] == 0


@pytest.mark.parametrize("rating", [-1, 1])
def test_both_valid_ratings_are_accepted(client, rating):
    ask_id = _record()
    assert client.post("/feedback", json={"id": ask_id, "rating": rating}).status_code == 201


def test_the_note_is_length_capped(client):
    """**The project's first unauthenticated write** (§4 of the spec names it).

    Capped at the same length as `AskRequest.question`, so the one thing a
    stranger can put into the database is bounded before it is stored.
    """
    ask_id = _record()

    too_long = client.post(
        "/feedback", json={"id": ask_id, "rating": 1, "note": "x" * 1001}
    )
    assert too_long.status_code == 422

    at_the_limit = client.post(
        "/feedback", json={"id": ask_id, "rating": 1, "note": "x" * 1000}
    )
    assert at_the_limit.status_code == 201


def test_a_broken_store_is_a_503_and_not_a_201(client, monkeypatch):
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(history, "record_feedback", explode)

    response = client.post("/feedback", json={"id": "anything", "rating": 1})

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "OperationalError" in response.json()["error"]


def test_the_mark_is_surfaced_in_the_history_data(client):
    ask_id = _record()
    client.post("/feedback", json={"id": ask_id, "rating": -1, "note": "missed a join"})

    answers = client.get("/history/data").json()["answers"]
    row = next(a for a in answers if a["id"] == ask_id)

    assert row["feedback"] == [
        {"rating": -1, "note": "missed a join", "created_at": row["feedback"][0]["created_at"]}
    ]


def test_an_unmarked_answer_carries_an_empty_list(client):
    """Present and empty, not absent.

    A reader that has to test for the key's existence will one day forget, and
    the failure is a crash on the history page rather than a missing tag.
    """
    _record()
    answers = client.get("/history/data").json()["answers"]
    assert answers[0]["feedback"] == []


def test_the_history_read_still_fails_loudly_when_the_store_is_broken(client, monkeypatch):
    """T7's asymmetry, re-checked now that a third query joins the page.

    `feedback_for_ids` raising must produce a 503, not a page of answers with
    the marks silently missing — which would read as *nobody has marked
    anything*, the same lie an empty answer list would tell.
    """
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: feedback")

    monkeypatch.setattr(history, "feedback_for_ids", explode)

    response = client.get("/history/data")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "no such table" in response.json()["error"]


# --- AC14: nothing aggregates it --------------------------------------------


def strip_js_comments(source: str) -> str:
    """Remove comments so an absence assertion reads code, not commentary.

    The repository's most repeated bug, in five costumes, is a test asserting a
    string is absent that matches the comment promising the absence — and the
    comments below this line in `history.js` and `app.js` are precisely
    paragraphs about not counting anything, using the words "count", "rate" and
    "percentage".

    `//` is stripped only at the start of a line, because
    `"http://www.w3.org/2000/svg"` lives in `app.js` and a general rule would
    eat the rest of that line.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.lstrip().startswith("//")
    )


def test_the_comment_stripper_did_not_gut_the_files():
    """The guard that makes every absence assertion below mean something.

    An over-matching regex makes them all pass. Landmarks are named rather
    than a size ratio being asserted: `api/web/history.js` is more than half
    comment by design, so a proportion test here would fail on correct code —
    which is the trap the AC13 vacuity guard hit in Iteration 7.
    """
    for name, landmarks in (
        ("history.js", ("function renderFeedback", "answer.feedback", "mark.rating")),
        ("app.js", ("function sendFeedback", "/feedback", "currentAnswerId")),
    ):
        stripped = strip_js_comments((WEB / name).read_text(encoding="utf-8"))
        for landmark in landmarks:
            assert landmark in stripped, f"the stripper removed {landmark!r} from {name}"


def test_ac13_the_page_posts_the_answer_id_with_the_rating():
    """**A surviving mutation: deleting `id` from the request body stayed green.**

    Nothing in this suite executes `app.js` — there is no browser and, by
    charter, no build step or test runner for the page — so the only available
    check on the request it sends is structural. Removing `id: currentAnswerId`
    from the `fetch` body left every other test passing, including the stripper
    guard, because `currentAnswerId` is still referenced by the early return
    above it.

    The consequence would not have been a visible break: `POST /feedback` would
    return `422` for a missing field, the page would show "Not recorded: …",
    and marking would simply never work. That is the shape of defect this
    project has caught twice before — code with tests and no working caller.

    AC13 is that the mark attaches to the **answer id**, so the id being in the
    body is the criterion, not a detail.
    """
    stripped = strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))

    body_lines = [line for line in stripped.splitlines() if "JSON.stringify" in line]
    assert body_lines, "app.js sends no JSON body to /feedback"

    feedback_body = [line for line in body_lines if "rating" in line]
    assert feedback_body, "no request body carries a rating"
    for line in feedback_body:
        assert "id" in line and "currentAnswerId" in line, (
            f"the mark must name the answer it belongs to; body is {line.strip()!r}"
        )


def test_ac13_the_answer_page_offers_the_control():
    """A user must be able to mark an answer, not just an API client.

    The endpoint having no caller is the failure Iteration 7 T6 caught as *"a
    cache with good tests and no callers"*. Asserted against the markup and the
    script together, because either one alone is inert: the buttons need a
    handler and the handler needs something to bind to.
    """
    markup = (WEB / "index.html").read_text(encoding="utf-8")
    for element_id in ("feedback", "feedback-good", "feedback-bad"):
        assert f'id="{element_id}"' in markup, f"no #{element_id} on the answer page"

    stripped = strip_js_comments((WEB / "app.js").read_text(encoding="utf-8"))
    assert 'getElementById("feedback-good")' in stripped
    assert 'getElementById("feedback-bad")' in stripped
    assert stripped.count("addEventListener") >= 1
    assert "sendFeedback(1)" in stripped, "the good button must send 1"
    assert "sendFeedback(-1)" in stripped, "the bad button must send -1"


@pytest.mark.parametrize("name", ["history.js", "app.js"])
def test_ac14_the_page_scripts_never_reduce_the_marks(name):
    """No count, no rate, no score, in code rather than in prose.

    Each is one line from `renderFeedback`, and each would turn a list of what
    people thought into a number that looks like a measurement of the system.
    §2.6 measured zero bad answers, so the first such number this project could
    compute would read *100% good* over a sample with no failures in it.

    **The first version of this test banned `marks.length` outright and failed
    on correct code.** `if (!marks.length) return null;` is an emptiness check,
    not an aggregate, and a rule that forbids asking *whether there are any*
    would have been satisfied only by contorting the renderer to please a test.
    So the ban is split along the line that actually matters:

    - `filter` and `reduce` over the marks are banned outright. Neither has a
      use here that is not aggregation.
    - a *length* is banned only where the same line renders something — a
      `textContent`, an `el(...)`, or a template literal. Counting privately to
      decide whether to draw a row is fine; putting the count in front of a
      reader is the thing AC14 forbids.
    """
    stripped = strip_js_comments((WEB / name).read_text(encoding="utf-8"))

    for banned in (
        "feedback.filter",
        "feedback.reduce",
        "feedback.map(",
        "marks.filter",
        "marks.reduce",
    ):
        assert banned not in stripped, (
            f"{name} reduces the marks with {banned!r}; AC14 forbids an "
            f"aggregate of feedback anywhere in the UI"
        )

    renders = ("textContent", "el(", "`")
    for line in stripped.splitlines():
        counts_marks = "marks.length" in line or "feedback.length" in line
        if counts_marks and any(token in line for token in renders):
            raise AssertionError(
                f"{name} renders a count of the marks: {line.strip()!r}. AC14 "
                f"allows asking whether there are any and not saying how many."
            )


def test_ac14_the_api_never_reduces_the_marks():
    """The same ban server-side, against the parsed AST.

    Asserted structurally because `api/main.py` and `api/store/history.py` both
    carry long docstrings explaining what they must not compute, using every
    word a textual search would look for.

    What this looks for: any `len()`, `sum()`, `count()` or comparison applied
    to something named after feedback. The check is deliberately about the
    *names*, since that is what a future aggregate would be built from.
    """
    for relative in ("api/main.py", "api/store/history.py"):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"len", "sum", "max", "min", "sorted"}:
                continue
            for argument in node.args:
                rendered = ast.dump(argument)
                assert "feedback" not in rendered.lower() and "marks" not in rendered.lower(), (
                    f"{relative}:{node.lineno} applies {node.func.id}() to the "
                    f"marks; AC14 forbids aggregating feedback in the API"
                )


def test_ac14_the_history_payload_carries_no_derived_feedback_field(client):
    """The payload's shape is the last line of defence.

    A `feedback_count`, `feedback_score` or `useful_rate` key would let any
    future client show an aggregate without touching the code AC14's other
    tests guard. The list is the whole contract.
    """
    ask_id = _record()
    client.post("/feedback", json={"id": ask_id, "rating": 1})
    client.post("/feedback", json={"id": ask_id, "rating": -1})

    row = client.get("/history/data").json()["answers"][0]

    derived = [
        key
        for key in row
        if "feedback" in key and key != "feedback"
    ]
    assert derived == [], f"derived feedback fields in the payload: {derived}"

    assert isinstance(row["feedback"], list)
    assert len(row["feedback"]) == 2, "both marks are present, unreduced"


def test_ac14_the_history_summary_says_nothing_about_feedback():
    """The reader's summary panel is where an aggregate would naturally land.

    It already computes totals over tokens and latency, so it is the one place
    in this codebase where adding "and 82% were useful" would look consistent
    with the surrounding code rather than out of place.
    """
    stripped = strip_js_comments((WEB / "history.js").read_text(encoding="utf-8"))
    summary = stripped[stripped.index("function renderSummary") :]
    summary = summary[: summary.index("function stat")]

    assert "feedback" not in summary, (
        "renderSummary must not mention feedback; a rate beside the token "
        "totals is the accuracy claim 009 AC13 kept off the page"
    )
