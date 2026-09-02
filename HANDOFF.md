# HANDOFF — context for a new session

This file exists so a fresh assistant session can pick QueryPilot up without
re-deriving anything. It is **not** a summary of the code; the specs are that.
It is the things that are *not* written down anywhere else: the standing rules,
the working rhythm, the measured state, and the mistakes that cost real time.

---

## 1. Read these first, in this order

| File | Why |
|---|---|
| [`specs/000-project.md`](specs/000-project.md) | The charter. §4 safety rules and §5 architectural commitments bind every iteration |
| [`EVALS.md`](EVALS.md) | Every measured number, with its caveats. Append-only |
| [`specs/008-prompt-tuning-plan.md`](specs/008-prompt-tuning-plan.md) | The next thing to build, approved and unstarted |
| This file, §2 and §6 | The rules, and the traps |

Each iteration has a spec (`NNN-name.md`) and a plan (`NNN-name-plan.md`). The
spec says *what and why* with acceptance criteria; the plan says *how* with a
task decomposition. Both carry a resolved-decisions block in the header.

---

## 2. Standing constraints — these are not negotiable

Stated by the user at the outset and reinforced since. Quoted, not paraphrased:

1. **"No LangChain, LlamaIndex, or agent frameworks. The agent loop is
   hand-rolled Python — that's deliberate."**
2. **"Keep the LLM provider behind a swappable interface."** One method,
   `complete(system, user) -> str`. No vendor SDK outside `api/llm/`. A
   structural test enforces it.
3. **"Never write code that bypasses the safety layer in section 3."** No code
   path executes SQL without `execute_sql()`, which runs Gate 2 first — not
   tests, not scripts, not `run_evals.py`. There is exactly **one recorded
   exemption**, documented in `specs/000-project.md` §4.
4. **"If a requirement is ambiguous, ask me instead of assuming."**

Added later, and equally binding:

5. **Test DSNs come from `TEST_DATABASE_URL`**, with a localhost default in
   `tests/conftest.py` only. *"Reject any test that embeds `localhost:5432`
   directly."*
6. **`tools.py` is a registry, not an implementation.** Logic lives in its
   domain module. *"Apply this pattern to all future tools."*
7. **The API key never appears in chat, a commit, a log, or an error message.**
   `GroqProvider._safe_message` scrubs defensively.

---

## 3. Working rhythm

The user chose this explicitly — *"I draft, you edit and own"* — and it has run
five times without variation:

1. Assistant drafts `specs/NNN-name.md` with acceptance criteria and **open
   questions Q-A…Q-E**, and presents it *before writing code*.
2. User answers the questions and approves.
3. Assistant drafts `specs/NNN-name-plan.md` with a task decomposition and
   **decisions D-1…D-3**.
4. User approves and answers.
5. Assistant implements task by task, running **mutation tests** at each step.
6. Assistant reports, including what the plan got wrong.

**Measure before specifying.** Every spec's §2 contains numbers taken from the
live database or real model calls, not estimates. Several specs changed shape
because a measurement contradicted the premise.

---

## 4. Where things stand

| Iteration | State |
|---|---|
| 0 Foundation | Done — Docker Compose, Chinook seed, read-only role |
| 1 Tools | Done — `get_schema`, `validate_sql`, `execute_sql`, `sample_rows` |
| 2 Single-shot | Done — one call, schema in prompt, through the safety layer |
| 3 Evals | Done — 40 reference queries, execution accuracy, `EVALS.md` |
| 4 Agent loop | Done — hand-written ReAct loop, 3-call budget, text protocol |
| **5 Prompt tuning** | **Specified and approved, not started.** Start at T1 |
| 6 Frontend | Not started |
| 7 Latency/cost | Not started |
| 8 CI | Not started |

**697 tests, 2 skipped** (live provider tests skip when rate-limited).

### The numbers that matter

- **97.5%** single-shot, full schema, dataset v2, `gpt-oss-120b`
- **82.5%** loop, schema withheld, `gpt-oss-20b` — against **0.0%** for the
  one-call control. That contrast is Iteration 4's whole justification.
- The loop shows **no gain** on the full-schema benchmark, because Iteration 3
  measured 100% Gate 2 pass and 100% execution rate — there is nothing for a
  retry loop to repair. This is stated plainly in `EVALS.md` rather than papered
  over.

### Immediate gotcha

The Groq free tier allows **200,000 tokens/day**. A blind-harness pass costs up
to ~98,000. Iteration 4's last runs were degraded by rate limiting — several
`EVALS.md` numbers are floors, not measurements, and say so. Iteration 5's AC5
and AC8 exist because of this.

---

## 5. Environment

```bash
# Prerequisites: Docker Desktop running; .env present (gitignored)
cp .env.example .env          # then add GROQ_API_KEY
./db/fetch_chinook.sh          # or db\fetch_chinook.ps1 on Windows
docker compose up -d

.venv/Scripts/python.exe -m pytest tests/ -q          # 697 tests, ~70s
.venv/Scripts/python.exe -m evals.run_evals --help
```

`QUERYPILOT_DATABASE_URL` must be set for host runs; the eval runner loads
`.env` itself, the test suite loads it via `conftest.py`.

---

## 6. Traps that cost real time — do not rediscover these

**Mutation testing is not optional.** It has caught defences that a fully green
suite hid, every single iteration. The recurring shape: *a default elsewhere in
the system silently stands in for the code under test.* Examples that actually
happened:

- `SET LOCAL statement_timeout` was masked by the role's own 10s default; the
  test only discriminated after monkeypatching the constant to 250ms.
- The `Decimal(str(x))` test used `0.1 + 0.2`, which quantises identically under
  both constructions. It proved nothing until the value became `4.0000005`.
- Ignoring the `ordered` flag left all 37 loop tests green, because the test's
  "wrong order" case used `ORDER BY ... ASC`, which returns *different rows*.

**A structural test that greps source will match its own docstring.** This
happened **twice** before the lesson stuck. Always assert against the parsed AST
(`ast.walk`), never `"foo" in source`.

**A completeness check that exempts its own module is not a completeness check.**
`RETRY_POLICY`'s test enumerated only upstream categories; the first category it
missed was one added in the same file, and the loop silently ended every run
containing a malformed action.

**sqlglot's node taxonomy is not intuitive.** `exp.Drop`, `exp.Alter`,
`exp.TruncateTable`, `exp.Set`, `exp.Grant` are **not** subclasses of
`exp.DML`/`exp.DDL`/`exp.Command`. Catch `SqlglotError`, not `ParseError` —
`TokenError` is a sibling, not a subclass. `count(DISTINCT x)` parses as
`Count(this=Distinct(...))`, not a `distinct=True` argument.

**Windows/shell specifics.** Write prose-heavy files with the Write tool, not
bash heredocs — apostrophes and backslash escaping mangle them repeatedly. Check
line endings after any scripted file edit; the repo is LF throughout.

**Chinook is arithmetically consistent.** `sum(invoice.total)` equals
`sum(line.unit_price * quantity)` exactly, so the textbook "what does revenue
mean" ambiguity is untestable here. Population definitions (active vs all
customers) discriminate; metric definitions do not.

---

## 7. Benchmark integrity — the rule that matters most

`EVALS.md` is **append-only**. Bad numbers stay. A regression quietly deleted
destroys the value of the whole record.

**Never edit an eval question because the model got it wrong — in either
direction.** When two questions were found to be defective *after* the score was
seen, they were **retired with new ids** (`medium-008` → `medium-017`,
`medium-016` → `medium-018`) and the reasons recorded, rather than edited in
place. The loader now refuses to reuse a retired id, so the rule is enforced
rather than merely stated.

The corollary — and the reason Iteration 3's number is trustworthy — is the
**ordering discipline**: all questions and gold queries are written and verified
*before* the runner exists. It caught three genuinely broken questions before any
model saw them. Iteration 5 extends this with a held-out dev/test split, locked
before any prompt tuning begins.

---

## 8. Starting Iteration 5

Everything needed is in
[`specs/008-prompt-tuning-plan.md`](specs/008-prompt-tuning-plan.md). It is
approved; begin at **T1**.

Three decisions in its §10 are still open — **D-1** (how token usage reaches the
runner), **D-2** (pre-registering the A/B decision rule before running it), and
**D-3** (whether the test-split audit trail is a strong enough guard). Ask
before assuming; that is rule 4.

Task order is load-bearing: **T4 (freeze the split) and T6 (author the expert
questions) both land before T7 (tune).** The split must be fixed before anything
is tuned against it, and the questions must be written before their score is
known.
