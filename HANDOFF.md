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
| [`specs/008-prompt-tuning-plan.md`](specs/008-prompt-tuning-plan.md) | Iteration 5, delivered. Read it for the working method, not for pending work |
| [`specs/010-hardening.md`](specs/010-hardening.md) and its plan | Iteration 7, delivered 2026-09-10. Its §2 holds the latency, cost and quota measurements |
| §4 of this file, and §8 of the charter | Where things stand, and what is next: **Iteration 8** (CI, plus AC6's deferred feedback) |
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
| **5 Prompt tuning** | **Closed 2026-09-04**; its last open criterion, AC13, satisfied 2026-09-08 as B-2 |
| **6 Frontend** | **Done 2026-09-09** — `POST /ask`, a page at `:8000`, all 14 ACs met |
| **7 Hardening** | **Done 2026-09-10** — T1-T7; feedback deferred to 8 (T1) |
| **8 Ship** | **In progress from 2026-09-10** — CI, B-9, B-10, feedback. Deployment and the demo video deferred as B-11/B-12 |

**1,107 tests** (live provider tests skip when rate-limited, which is a
working guard rather than a red failure -- see the traps below). The suite
runs in ~95s, down from ~144s: T6's schema cache removed most of the
introspection it was paying per test.

### What Iteration 7 added, and the surface it left

Five endpoints and two pages, all served by the one container:

| | |
|---|---|
| `POST /ask` | answers, and now returns `usage`, `total_ms`, `provider_ms`, `cache_hit`, `id` |
| `GET /` | the answer page, with the quota banner and the cached-answer note |
| `GET /history` | the reader: every question, its cost, its trace |
| `GET /history/data` | the same as JSON; **503 when the store is unreadable**, never an empty list |
| `GET /quota` | what the provider last said about its limits |
| `GET /health` | now also reports `history.writable`, and stays 200 when it is false |

Operational state lives in **SQLite at `/data/querypilot.db`** in the
`querypilot_data` named volume. `docker compose down` keeps it; only `down -v`
discards it. Verified across a real machine shutdown: 23 rows survived.

**Three caches now exist and they are not the same thing.** Confusing them is
the easiest way to misread this code:

| | keyed on | invalidated by | shared with `evals/` |
|---|---|---|---|
| answer cache (`api/http/cache.py`) | question + schema fp + prompt fp | a schema or prompt change | **no** — D-1, and a test enforces it |
| schema cache (`api/db/schema_cache.py`) | nothing; one slot | a 5.3ms catalog probe | yes, deliberately |
| quota snapshot (`api/http/quota.py`) | nothing; one slot | its own age vs the bucket's reset | n/a |

**The answer cache does not notice a data change.** Its key covers the question,
the schema and the prompt, so an added *column* invalidates an entry and an
added *row* does not. Chinook is static so it never bites here; the page and the
README both say so rather than leaving it to be discovered.

### The backlog board, in `specs/000-project.md` section 8

| | | |
|---|---|---|
| ~~B-1~~ | rate-limit telemetry and pacing | discharged 2026-09-04 |
| ~~B-3~~ | T8's held-out run | discharged 2026-09-04 |
| ~~B-5~~ | three-limit guards and the daily ledger | verified live 2026-09-08 |
| ~~B-2~~ | AC13's glossary-off control | discharged 2026-09-08 -- see section 8 |
| **B-4** | alternative LLM provider | deferred, own milestone |
| **B-6** | 429 to ledger reconciliation, live | open, accepted debt |
| **B-9** | AC14's live injection test asserts a model behaviour | open, filed 2026-09-10 |
| **B-10** | `get_schema()` reaches the database around Gate 2 | open, filed 2026-09-10 |
| **B-11** | production deployment | deferred at Iteration 8 T1 — a decision, not a task |
| **B-12** | demo video | deferred at Iteration 8 T1 — not code, and the system is still moving |
| ~~B-7~~ | which `expert` questions the glossary rescues | discharged 2026-09-09 |
| ~~B-8~~ | `naive_sql` records an assumption AC12 cannot check | discharged 2026-09-09 |

### The numbers that matter

- **100.0%** held out — `compact` + glossary, `--split test`, 20/20, the only
  Iteration 5 entry in `EVALS.md`. **Read it as one pass, not as the
  accuracy**: eight passes of the identical configuration produced 0 to 2
  wrong answers each, so the honest statement is *between 90% and 100%,
  measured once at 100%*.
- **`007`'s claim of a 0.0% spread across three passes does not survive**
  that. AC18's *a difference of one question is a real difference* needs
  recalibrating: on a 20-question split the noise is at least two.
- **D-2 adopted `compact`** on a 0.5-question dev margin — inside that noise.
  The adoption stands because `compact` was never *worse* and is **188
  measured tokens a call cheaper**. It is cheaper and not worse; it is *not*
  more accurate, and any text implying otherwise is overclaiming.
- **`expert` 7/12 without the glossary against 6/6 with it** (B-2, three `ddl`
  dev passes). The tier-level split is the finding; the overall spread
  (96.7 / 93.3 / 90.0) is inside the noise and proves nothing on its own.
  Both control passes were **24/24 on every other tier**, so the glossary's
  whole measured effect is in `expert` — which is what it was built for. It
  costs **179 measured tokens a call**. Unlike `compact`, this one *is* an
  accuracy claim, and it is a claim about one six-question tier.
- **The glossary rescues exactly `expert-001`, `expert-003`, `expert-004`**
  (B-7, two verbose passes, same three both times with the same wrong values).
  Pooled across all four glossary-off passes the tier is **13/24**. Quote the
  named set rather than the tier percentage: the percentage moves with noise,
  the set did not.
- **Latency is bimodal and the slow mode is silent** (010 §2.3). ~750-1,250ms
  for one user at a time; up to **10,393ms** under sustained load, with the work
  held constant at ~1,100 tokens. There is no 429 and no header — the provider
  slows down as the 8,000/minute bucket drains. `GET /quota` and the page's
  banner exist because nothing in the product noticed this before.
- **A question costs ~1,100 tokens and one provider call** (010 §2.5). Median
  1,078, range 1,047–1,256. At 200,000 a day that is ~180 questions, and the
  minute bucket allows about **seven in any sixty seconds** before the slow mode.
- **The provider is 94.2% of wall clock**, so `total_ms` and `provider_ms` are
  recorded apart. Everything else was 6% before T6 and is now ~3%.
- **T6, measured in the container**: `get_schema()` is **52 round trips at 99ms**;
  the catalog probe that replaces it is **3 round trips at 5.3ms**. A cache hit
  went 113ms → **6ms**; a warm miss's non-provider gap went 272ms → **22ms**.
- **T4, measured live**: six identical questions fired at once cost **one**
  provider call. Four hits and two misses summed to exactly the billed total
  with no filtering, because a hit records `usage = 0` rather than replaying
  what the original cost.
- **97.5%** single-shot, full schema, dataset v2 — the Iteration 3 baseline.
- **82.5%** loop, schema withheld, `gpt-oss-20b`, against **0.0%** for the
  one-call control. Iteration 4's whole justification. That figure is a
  floor: 33 correct and 7 rate-limited.

### The rate limits, measured — there are three, and one is invisible

| limit | capacity | reported where |
|---|---|---|
| tokens per minute | 8,000 | headers |
| requests per day | 1,000 | headers |
| **tokens per day** | **200,000** | **only a 429 body** |

B-1 measured these and B-5 guards them. Two things worth carrying:

- **Absence of a header is not absence of a limit.** B-1's first conclusion was
  that the 200,000 daily figure was unenforced because nothing reported it. It
  is enforced; a refusal arrives with the *minute* bucket reading a full
  8,000/8,000 and a body naming `tokens per day (TPD)`. That mistake is
  preserved in the charter's B-1 entry rather than tidied away.
- **The worst-case projection is roughly 3x reality**, because AC8 assumes three
  calls a question and the loop uses one. It has already refused work it should
  have allowed. Real dev-split runs cost ~35,000–40,500 tokens.

`evals/ledger.py` tracks the day's spend in `.querypilot/spend.json`
(gitignored, UTC-keyed). It is a **floor**: it sees only what the eval runner
spent, not the API or another checkout. `PacedProvider` keeps runs under the
minute bucket — a 30-question run takes ~20–29 waits and ~150 seconds of
sleeping, and no rate limits.

Iteration 4's last runs were degraded by rate limiting — several `EVALS.md`
numbers are floors, not measurements, and say so.

---

## 5. Environment

```bash
# Prerequisites: Docker Desktop running; .env present (gitignored)
cp .env.example .env          # then add GROQ_API_KEY
./db/fetch_chinook.sh          # or db\fetch_chinook.ps1 on Windows
docker compose up -d

.venv/Scripts/python.exe -m pytest -q                 # 1,107 tests, ~95s
.venv/Scripts/python.exe -m evals.run_evals --help
```

`QUERYPILOT_DATABASE_URL` must be set for host runs; the eval runner loads
`.env` itself, the test suite loads it via `conftest.py`.

**The history store is inside the container, in a volume.** `/data/querypilot.db`
in `querypilot_data`, so a host tool cannot open it directly — read it at
<http://localhost:8000/history>, or:

```bash
docker compose exec api python -c "from api.store.history import recent; print(len(recent()))"
```

`docker compose down` keeps that volume. **`down -v` destroys it**, along with
`pgdata` — which is still the documented recovery for a half-initialised
Postgres, so it is worth knowing that it also discards the question log.

**`.env.example` did not carry that key until Iteration 5 closed**, so a
host run of `python -m evals.run_evals` failed with a
`SchemaIntrospectionError` naming a variable that nothing set. Compose
supplies it to the *container* as `@db:5432`, which the host cannot reach;
from the host it is the read-only role on the mapped port. Copy the line
from `.env.example` and fill it in, or the documented command in the README
will not run outside Docker.

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

**A test can assert a *mention* instead of a *use*, and pass.** Three mutations
survived this shape in Iteration 7 before the tests were fixed. A page test
checked that `quota.note` appeared somewhere in `app.js`, and a hardcoded banner
string left it green because the guard clause above the assignment still
mentioned the name. Another checked that `renderCacheNote` was *defined* rather
than *called*. A third claimed to cover `observe(None)` and never reached that
path at all, because a guard higher up returned first. **Assert the call, the
assignment, or the effect — never that an identifier is present in a file.**

**A cache with good tests can still be dead code.** Reverting the request path
to raw `get_schema()` left all nineteen `test_schema_cache.py` tests green,
because every one of them exercised `cached_schema()` directly. Adoption needs
its own assertion: count the round trips end to end through the endpoint, and
name the modules structurally so a failure says which one regressed.

**A page that serves is not a page that renders.** Every assertion about the
JavaScript read it as text — no `innerHTML`, no CDN, the right names present —
and every one of them passes on a file with a syntax error in it, which returns
200, renders blank, and reports itself only to a console nobody is watching.
The suite now runs `node --check` over `app.js` and `history.js`, skipping where
node is absent.

**A vacuity guard measured in bytes punishes commentary.** The AC13
comment-stripper's guard required the stripped page to exceed half the raw file,
and adding two well-commented sections took the page to 55% comments, which it
read as an over-matching regex. Length was only ever a proxy for *did real
markup survive*; it now names the elements that must survive, and still fails on
a genuinely greedy regex.

**The absence-assertion trap has now appeared five times, and the fifth was not
a comment.** The history page's footer disclaimer said the counts are "not an
accuracy figure" — and a page asserted not to contain the word cannot carry a
disclaimer built from it. Stripping comments was no help; the copy had to
change. Then the test failed again on its own strictness, matching a phrase
across a line break. Collapse whitespace before asserting on rendered text.

**SQLite: applying the schema on every connection is a write lock.**
`executescript()` takes one even for a reader, which produced `database is
locked` on ~0.4% of writes at 16 threads — rare enough to pass a suite, frequent
enough to drop real telemetry. **`BEGIN IMMEDIATE` made it worse**, because
taking the lock earlier moves contention rather than removing it. The fix is
schema-once-per-path plus an in-process write lock, which is sound because there
is exactly one API process.

**A fingerprint over a truncated result is stable and blind.** Gate 3 caps
results at 1,000 rows. The catalog probe returns 91 today, but a two-hundred-
table warehouse would truncate — and a hash of the first thousand rows of a
stable catalog never changes while missing everything after them. `truncated`
has to mean *unverifiable*, not *unchanged*.

---

**Shared state a test can reach will eventually be written by one.** It has
happened twice, and Iteration 7 added four more places it could. T5's `EVALS_PATH` was bound as a default argument, so
`monkeypatch` had no effect and a mutation run filed a fake entry in the real
`EVALS.md`. B-5's spend ledger was then written by two tests that drive `main()`
end to end and had no reason to know a ledger existed — gitignored, so invisible
in review, and read by the next real run's pre-flight. Per-test discipline
failed both times; isolation is now an autouse fixture. **Resolve paths at call
time, and isolate shared state for every test whether it asks or not.**

`tests/conftest.py` now carries six autouse isolators: the spend ledger, the
history store, the answer cache, the quota snapshot, and the schema cache. The
last one is the only one that can hold something *false* rather than merely
stale — a test that monkeypatches `get_schema` leaves a hand-built `Schema`
behind, and the next test builds its prompt from a database that does not
exist.

**The recorded path and the terminal path drift apart.** Four defects of one
shape reached `EVALS.md` or its report before anyone noticed: the recorded block
read `reports[0]` where it had to read the whole run — for the token total, for
the rate-limit guard, and for held-out failure detail that D-3 says must be
withheld. Nothing exercised `run_evaluation` at `repeat > 1` all the way to a
file. `tests/test_multi_pass_recording.py` exists for exactly that seam.

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

## 8. Historical: B-2, discharged — and the one question it left

> Kept for the reasoning, not because it is current. Iteration 7 closed after
> this; §4 is where things actually stand.

**Closed 2026-09-08. Nothing here is waiting on a decision.** AC13 asked for
accuracy with and without the glossary. Three passes ran on `--split dev`, all
`ddl`, so the arms differ in exactly one bit:

| arm | pass | overall | easy | medium | hard | **expert** | failures | tokens |
|---|---|---|---|---|---|---|---|---|
| `ddl` + glossary | 1 | 96.7% | 8/8 | 9/10 | 6/6 | **6/6** | 1 `no_sql_returned` (`medium`) | 40,502 |
| `ddl` no glossary | 1 | 93.3% | 8/8 | 10/10 | 6/6 | **4/6** | 2 `wrong_result` (`expert`) | 34,950 |
| `ddl` no glossary | 2 | 90.0% | 8/8 | 10/10 | 6/6 | **3/6** | 3 `wrong_result` (`expert`) | 34,901 |

**The headline proves nothing** — 96.7 / 93.3 / 90.0 sits inside the documented
0–2 noise, and anyone quoting the 6.7-point gap as the result is overclaiming.
**The tier breakdown is the finding**: `expert` is the only tier that moves, and
it is the only tier the glossary is supposed to touch. The failure *categories*
follow the mechanism — with the glossary the single miss is a generation hiccup
in `medium`; without it every miss is `wrong_result` in `expert`, which is what
a naive reading of an ambiguous term produces rather than what a broken query
does.

**The follow-up was thought to be blocked by our own guard, and it was not.**
The earlier reading quoted a single worst-case projection (102,240) for both
arms and concluded that either arm would be refused at 213,003 against the
200,000 ceiling. **The projection is per-arm, and the two arms differ**: the
glossary block is 179 tokens a call, so over 30 questions at a worst case of
three calls each the control arm projects **86,130**, not 102,240. Against a
ledger of 110,763 that is 196,893 — inside the limit. Only the treatment arm
was ever refused.

The lesson generalises past this run: **a projection that varies with the
configuration must be recomputed per arm, not quoted once for a matrix.** The
figure had been carried forward as though it described the experiment rather
than one arm of it.

The user chose the control arm, and it ran on 2026-09-08 with the daily guard
untouched at its measured 200,000. `--max-projection 90000` raised only *this
run's own* pre-flight ceiling, which is what allows a `--token-budget 50000`
breaker to exist at all — the two guards are denominated differently and a
single number makes one of them vacuous. The daily guard is unaffected by that
flag; it compares the real projection against `--daily-token-limit`.

**The deficit replicated and deepened, and it is confined to one tier.** Across
both control passes every non-`expert` question is correct — 24/24, twice — and
all five failures are `wrong_result` in `expert`. Pooled, glossary-off `expert`
is **7/12**; glossary-on `expert` is 6/6 in the matched arm and 6/6 again in
T7's dev run and B-5's 30/30 dev run. Those two corroborating runs are
glossary-on but **not necessarily `ddl`** — T7's dev run was `compact`, and
B-5's rendering was never written down — so they corroborate the glossary bit
while varying a second one. The clean single-variable comparison is the matched
`ddl` pair in the table above.

**What it still does not establish, and this is the honest limit.** The tier
holds six questions, and two passes over the same six are not twelve
independent trials. More decisively, **neither control pass recorded which
questions failed**, so *the same two questions failing every time plus one
flake* and *the glossary lifting the tier broadly* both fit the data. Pass 2
was run without `--verbose`, which costs nothing and would have settled it;
that omission is the single thing to fix if a third pass is ever authorised.

**Do not quietly raise the daily limit.** It is a guard built this week, and
stepping over it is the user's call, not a convenience. A third control pass
projects 86,130 against a ledger now at 145,778, so it *is* refused — genuinely
this time.

### The rest of the board

**B-7 was discharged 2026-09-09.** Two `--verbose` glossary-off dev passes on a
fresh quota named the rescued set and found it stable: **`expert-001`,
`expert-003`, `expert-004`** fail both times, with the *same* wrong values both
times — 59, 2240, 204 against golds of 46, 1984, 165. `expert-001`'s SQL was
byte-identical between passes. The glossary does a narrow, nameable job rather
than lifting the tier broadly, and `expert-002`, `expert-007` and `expert-008`
are read correctly without it.

It also turned up something about the dataset rather than the model, now
carried as **B-8**: `naive_sql` predicts a failure the model does not make. It
assumes the term is ignored — `count(*) FROM artist` — where the model instead
picks a *different* restriction, counting the 204 artists with a catalogue
rather than the 165 with sales. Without the definition the model does not fail
to answer; it answers a different question, plausibly. That is a stronger case
for the glossary than the accuracy delta is.

**B-6** needs a 429 that names TPD, which only happens near the daily ceiling.
Accepted as debt; close it opportunistically the next time a run is refused in
the ordinary course of work, rather than burning ~165,000 tokens to reach a
state worth reaching.

**B-4** stays deferred as its own milestone. A model change retires every
recorded number at once, so it never rides along with other work.
