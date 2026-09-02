# 005 — Single-Shot Generation: Implementation and Test Plan

> **STATUS: COMPLETE.** 405 tests passing, 1 skipped.
> Spec: [`005-single-shot-generation.md`](005-single-shot-generation.md).
>
> **§1 needs your confirmation before anything else**: the model this iteration
> was specified against does not exist, so the default is an open decision again.
> §7 asks for that plus one more.

---

## 1. The model decision has reopened

Your Q1 answer was `llama-3.3-70b-versatile`. It returns **404
`model_not_found`**, and no Llama chat model is reachable on this key.

Measured across four questions each:

| Model | Gate 2 pass | Executed correctly | Latency (min / med / max) | Reasoning trace |
|---|---|---|---|---|
| `openai/gpt-oss-120b` | 3/4 | **3/3** | 0.60 / 0.70 / 0.84 s | separate field |
| `openai/gpt-oss-20b` | 3/4 | **3/3** | 0.23 / 0.59 / 0.64 s | separate field |
| `qwen/qwen3.6-27b` | 0/4 | 0/4 | 0.78 / 1.83 / 3.32 s | **inline `<think>`** |

*(The fourth question is the injection probe; both gpt-oss models refused it in
prose, which Gate 2 then rejected. 3/3 counts the three real questions.)*

**Recommending `openai/gpt-oss-120b`.** Both gpt-oss models were perfect on these
questions, and the latency gap — 0.70s versus 0.59s median — is not worth
choosing the smaller model for. The questions here are Iteration 2's
"simple single-table" bar; Iteration 5 will deliberately push harder ones, and
the larger model has more headroom for that. `gpt-oss-20b` is one environment
variable away if Iteration 3's eval runs prove slow.

This is **D-1** in §7, because it changes a decision you already made.

---

## 2. Approach

```
question
   ▼
get_schema()                          -> Schema                (Iteration 1)
render_schema_ddl(schema)             -> str                   (AC7-AC12)
build_prompt(schema_text, question)   -> (system, user)        (AC13-AC15)
   ▼
provider.complete(system, user)       -> str    ONE call       (AC16)
   ▼
extract_sql(response)                 -> str | None            (AC17-AC19)
   ▼
execute_sql(sql)                      -> ExecutionResult       (AC20-AC22)
   ▼
AnswerResult
```

Every guarantee from Iterations 0 and 1 is inherited. This module adds one
network call and one string-slicing step, and nothing else.

---

## 3. Extraction: the measured hazard is not the assumed one

The spec assumed markdown fences were the main problem. **Fencing was never
observed** from the default model across eight calls. What actually broke a model
completely was an inline reasoning trace:

```
qwen/qwen3.6-27b:  "<think>\nThe user wants to know the total number of
                    tracks...\n</think>\n\nSELECT COUNT(*) FROM track;"
                   -> Gate 2 rejects: "Error tokenizing ' FROM track;"
                   -> 0/4 questions, every one for this reason
```

`openai/gpt-oss-*` avoids this by putting reasoning in a **separate `reasoning`
field**, leaving `content` clean. That is a property of the model, not of the
provider or of our prompt — and AC3 makes swapping model an environment change,
so extraction must survive it.

**Proposed `extract_sql`, in order:**

1. strip a `<think>…</think>` block if present (measured: qwen);
2. strip markdown fences if present (measured: qwen once, gpt-oss never);
3. take the remainder, stripped;
4. if empty, return `None` → `no_sql_returned`.

Deliberately **not** doing: searching for the first `SELECT` keyword, or
regex-extracting a statement. That is pattern-matching on model output to decide
what to execute — one step from the keyword-blocklist mistake `002` exists to
avoid. If step 3 leaves prose, Gate 2 rejects it and the agent is told why, which
is the correct outcome. Measured: the refusal *"I'm sorry, but I can't help with
that"* takes exactly that path.

---

## 4. Files

| File | Status | Contents |
|---|---|---|
| `api/llm/__init__.py` | **new** | package marker |
| `api/llm/base.py` | **new** | `LLMProvider` protocol, `LLMError`, timeout constant |
| `api/llm/groq_provider.py` | **new** | **the only file importing the Groq SDK** |
| `api/llm/factory.py` | **new** | `get_provider()` — reads config, returns a provider |
| `api/agent/prompts.py` | exists, empty | `render_schema_ddl()`, `build_prompt()` — `001` Q-A put rendering here |
| `api/agent/single_shot.py` | **new** | `extract_sql()`, `answer_question()`, `AnswerResult` |
| `api/requirements.txt` | edit | add `groq` |
| `.env.example` | edited already | provider, model, key slot |
| `tests/test_prompts.py` | **new** | pure |
| `tests/test_single_shot.py` | **new** | fake provider; no network |
| `tests/test_llm_live.py` | **new** | 2–3 live tests, skipped without a key (Q-B) |

`api/agent/orchestrator.py` stays empty — it is the loop's file.

**Naming note:** `groq_provider.py`, not `groq.py`. A module named `groq.py`
inside a package that also does `import groq` is a shadowing accident waiting to
happen.

---

## 5. Structure

```python
# api/llm/base.py
LLM_TIMEOUT_SECONDS = 20          # D-2
class LLMError(RuntimeError): ...
class LLMProvider(Protocol):
    def complete(self, system: str, user: str) -> str: ...

# api/llm/groq_provider.py  — the only vendor import in the codebase
class GroqProvider:
    def complete(self, system, user) -> str:
        # temperature=0, timeout, model from config
        # any SDK exception -> LLMError, with the key never in the message
```

Three things to get right:

- **The key must not reach an error message (AC5).** SDK exceptions can carry
  request context. `LLMError` is raised with a message this code composes, and
  the original is chained but never formatted into the result.
- **`complete()` returns `content` only.** The `reasoning` field is deliberately
  discarded — it is model-shaped, and surfacing it would put a vendor concept
  into the abstraction. Iteration 5 can revisit if failure analysis wants it.
- **`get_provider()` fails loudly on a missing key**, before any call (AC4).

---

## 6. Test plan

**Pure, no network, no database** — `tests/test_prompts.py`:
- rendering includes every relation, kind, column types as `001` rendered them,
  PKs including composite, and FKs with direction (AC7–AC10)
- deterministic for a synthetic `Schema` (AC11)
- no sample values appear (AC12)
- the instruction names PostgreSQL, one SELECT, no DDL/DML (AC13)
- the question appears verbatim, unsanitised (AC14)

**Fake provider, no network** — `tests/test_single_shot.py`:

| Fake returns | Expected |
|---|---|
| clean SQL | executes, `ok=True` |
| SQL in ``` fences | fences stripped, executes |
| `<think>…</think>` + SQL | trace stripped, executes — the qwen case |
| `"I'm sorry, but I can't help with that."` | `no_sql_returned` — the measured refusal |
| `""` | `no_sql_returned` |
| `DROP TABLE track` | `rejected`, validator reason intact, **no retry** |
| raises `LLMError` | `provider_error` |

- exactly one `complete()` call per question, asserted by counting (AC16)
- `AnswerResult` carries question, SQL, and `ExecutionResult` (AC22)
- raw response captured **only on failure paths** (Q-E)

**Live, skipped without `GROQ_API_KEY`** — `tests/test_llm_live.py` (Q-B):
1. a simple question returns SQL that passes Gate 2 and executes
2. the response contains no `<think>` and no fences — pinning the default
   model's measured behaviour, so a model change that breaks the assumption
   fails here rather than silently in evals
3. an injection question does not produce executable DDL/DML

**Structural** — the one that keeps the swappability promise honest:
```python
# no module outside api/llm/ imports the vendor SDK   (AC2)
```

**Mutation checks:**

| Mutation | Expected |
|---|---|
| Call the engine instead of `execute_sql()` | a test proving generated SQL is validated goes red |
| Add a retry around `complete()` | the one-call assertion goes red |
| Drop `<think>` stripping | the qwen-shaped fake test goes red |
| Return `reasoning` concatenated with `content` | Gate 2 rejection tests go red |

---

## 7. Decisions I need from you

**D-1 — Confirm `openai/gpt-oss-120b` as the default model.** Your original
choice is unavailable (§1). The alternative is `gpt-oss-20b` for ~0.11s less
median latency. *My lean: 120b*, with 20b as the documented fallback if
Iteration 3's eval runs are slow.

**D-2 — Timeout value.** Measured max is **0.84s**. I propose **20 seconds** —
roughly 24× headroom, absorbing cold starts and harder Iteration 5 questions,
while still failing fast enough that a hung request does not tie up a worker for
half a minute. The spec's original guess was 30s, made with no data.

---

## 8. Risks

| Risk | Handling |
|---|---|
| Model availability changes again | AC3 keeps the model in configuration, not in code. The live test pins observed behaviour so a change fails loudly |
| Live tests make CI flaky or slow | Only 2–3, skipped without a key, and separate from the hermetic suite |
| An LLM failure looks like a SQL failure | `provider_error` and `no_sql_returned` are distinct categories (AC24) |
| Extraction becomes a parser | §3 — three deterministic strips and nothing more. Anything else is Gate 2's job |
| The baseline drifts before Iteration 3 measures it | Determinism measured at 1/5 distinct; AC16 forbids retries. Record the model and prompt version alongside the number in `EVALS.md` |

---

## 8b. Task log — ALL COMPLETE

| Task | Status | Outcome |
|---|---|---|
| **T1** | done | `api/llm/` — protocol, `LLMError`, `GroqProvider`, factory. Live smoke test passed |
| **T2** | done | `render_schema_ddl()` — pure, 12 tests |
| **T3** | done | `build_prompt()` — pure, 10 tests |
| **T4** | done | `extract_sql()` — `<think>` and fences, 11 tests |
| **T5** | done | `answer_question()` through `execute_sql`, 21 tests against a fake provider |
| **T6** | done | 3 live tests (skippable), `groq` in requirements, **compose env fixed** |

### Findings

**Compose never passed the LLM variables to the API container.** Discovered only
by running the container, not by any test: `docker compose exec api` produced
`provider_error: No Groq API key configured`. The failure was clean and the
message actionable — AC4 doing its job — but `docker-compose.yml` had no
`GROQ_API_KEY`, `QUERYPILOT_LLM_PROVIDER` or `QUERYPILOT_LLM_MODEL` entry. Fixed,
with an empty default so a missing key degrades one feature rather than stopping
the container from booting.

**A structural test failed on this module's own docstring — for the second
time.** `test_ac20` grepped the source for `validate_sql(`, which appears in the
`answer_question` docstring explaining that it deliberately does *not* call it.
The identical mistake was made and fixed in `003`'s bypass test; the lesson did
not carry forward. Both now inspect identifiers via AST rather than raw text.

---

## 9. Decomposition (as executed)

| # | Task | Verified by |
|---|---|---|
| **T1** | `api/llm/` — protocol, `LLMError`, `GroqProvider`, factory | AC1–AC6; live smoke test |
| **T2** | `render_schema_ddl()` — pure | AC7–AC12 |
| **T3** | `build_prompt()` — pure | AC13–AC15 |
| **T4** | `extract_sql()` — pure, all three shapes | AC17–AC19 |
| **T5** | `answer_question()` wired to `execute_sql` | AC16, AC20–AC25 |
| **T6** | Live tests + `groq` into requirements + container rebuild | Q-B |

T2–T4 are pure and need neither network nor database, so most of this iteration
is testable hermetically — which is what keeps the suite's current property of
running without an API key.
