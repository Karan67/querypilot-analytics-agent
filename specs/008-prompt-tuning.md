# 008 — Prompt Tuning, Token Cost, and Real-World Difficulty

> **STATUS: APPROVED — Q-A and Q-B settled 2026-09-03.**
> Plan: [`008-prompt-tuning-plan.md`](008-prompt-tuning-plan.md).
>
> **Resolved:** Q-A — a held-out split, locked before any tuning begins. Q-B —
> keep `api/db/sampling.py`, remove `sample_rows` from `TOOLS` and from the
> prompt. Q-C, Q-D and Q-E stand as leaned unless you say otherwise; the plan
> pre-registers Q-E's decision rule as its D-2.
>
> **§2 contains three measurements that change the shape of this iteration**, one
> of which contradicts the stated motivation for the first task. §7 Q-A is the
> most important question here and it is not technical — it is about whether
> tuning a prompt against the eval set leaves the number meaning anything.

Iteration: 5 (Accuracy) · Inherits: [`000-project.md`](000-project.md)
Depends on: [`006-evals.md`](006-evals.md) — the number — and
[`007-agent-loop.md`](007-agent-loop.md) — the thing being tuned.

---

## 1. Intent

Make the agent cheaper and harder to fool: strip the action nobody uses, stop
the schema dominating every prompt, and give the benchmark questions that
require knowing the business rather than knowing SQL.

> **Iteration 5 — Done when:** accuracy improves against Iteration 4's numbers,
> and the improvement is attributable.
>
> **CLOSED 2026-09-04, with one criterion knowingly unmet.**
>
> Settled: `sample_rows` is gone; the corpus is v3 with a frozen 30/20 split
> (`ec65d5ba81d6`); D-2 selected `compact`, which is the deployed default; and
> `EVALS.md` carries a held-out **100.0%** on 20 questions the tuning never
> touched, `expert` tier included.
>
> **Read that 100.0% as one pass, not as the accuracy.** Eight passes of the
> same configuration produced 0 to 2 wrong answers each (plan section 12), so
> the honest statement is *between 90% and 100%, measured once at 100%*. The
> iteration's own bar — *accuracy improves against Iteration 4 and the
> improvement is attributable* — is met on the first half and only partly on
> the second: `compact` is attributably **cheaper** (188 tokens a call), but
> the accuracy difference that selected it is inside the noise.
>
> **AC13 remains unmet**, deferred rather than satisfied — the glossary-off
> control was never run, so the 178-token block that ships on every call is
> still unjustified by measurement. It is carried as charter backlog **B-2**
> rather than quietly closed with the iteration.

---

## 2. What the measurements say

### 2.1 Removing `sample_rows` saves 19 tokens, not a meaningful share

> **Corrected 2026-09-03, at Iteration 5 T1.** The numbers first published here
> were **not measurements**. They were `len(text) // 4`, and the table below
> records both so the correction is visible rather than silent: 4120 chars →
> 1030, 3340 → 835, 271 → 67, 88 → 22. Three of the four matched the heuristic
> exactly, which is how it was identified.
>
> The figures now shown are counted with `tiktoken`, encoding `o200k_base` —
> the encoding of the deployed `openai/gpt-oss-*` family. `cl100k_base` was
> checked as a control and agrees to within 6 tokens on the whole prompt, so
> this is a real tokenizer disagreeing with a heuristic, not two tokenizers
> disagreeing with each other.

| Component of the loop system prompt | Tokens | Share | Was claimed |
|---|---|---|---|
| Schema DDL | 771 | **82%** | 835 |
| Rules and boilerplate | 109 | 12% | 128 |
| Action list (all three) | 62 | 7% | 67 |
| — of which `sample_rows` | **19** | **2.0%** | 22 |
| Whole prompt, schema rendered | 944 | 100% | 1030 |

The parts sum to 942 against a whole of 944; BPE merges across the join
boundaries account for the two.

**Every absolute number was ~9% too high, and every ratio survived.** The
argument this section makes rests on the shares — schema dominates at ~82%,
the retired action is ~2% — and those are what the correction leaves standing.

That is luck rather than vindication, and §2.2 proves it: when the same
heuristic was corrected there, **the ratios did not survive** — −37% became
−24% and −54% became −38%. An estimate that happens to preserve the ratio it is
quoted for is still an estimate, and the next one may not be so kind. The
prompt total here (944) is the pre-T1 prompt, which still offered
`sample_rows`; §2.2's 925 is the same prompt after T1 removed it.

**The evidence for removing it is real; the token argument is not.** It was
chosen zero times across 24 probe calls and every recorded Iteration 4 run, and
an option the model never takes is dead weight in its decision space. That is a
good enough reason on its own. But removing it will not move the token problem,
and this spec should not pretend otherwise — the 82% is where the money is.

### 2.2 The schema is 83% of the prompt, and compaction is cheaper than DDL but dearer than estimated

> **MEASURED at T2, 2026-09-03**, with `tiktoken`/`o200k_base` against the
> renderings now in `api/agent/prompts.py`. The estimates this section
> originally carried were the same `chars // 4` heuristic §2.1 corrects, and
> **the prediction made when they were flagged was wrong**: it said a
> proportional error would largely cancel in the ratio. It did not. The
> percentages moved substantially, in the direction that matters.

Three renderings of the same twelve relations:

| Rendering | Est. | **Measured** | Est. change | **Measured change** |
|---|---|---|---|---|
| DDL (current) | 835 | **771** | baseline | baseline |
| Compact, full types | 532 | **583** | −37% | **−24%** |
| Compact, abbreviated types | 389 | **481** | −54% | **−38%** |

Assembled into the whole loop system prompt, which is what actually gets billed:

| Rendering | Prompt | vs DDL | One 40-question pass | Share of the 200k/day ceiling |
|---|---|---|---|---|
| `ddl` | 925 | — | 37,000 | 18.5% |
| `compact` | 737 | **−20.3%** | 29,480 | 14.7% |
| `compact-abbrev` | 635 | **−31.4%** | 25,400 | 12.7% |

**Two reasons the measured saving is smaller than the estimate**, both
deliberate rather than accidental:

1. **The compact form carries more than the sketch did.** The T2 decision kept
   view marking (+3), nullability (+10) and explicit foreign-key target columns
   (+24) — 37 tokens — on the grounds that Chinook is the fixture and not the
   deployment target: a real schema may key on a non-primary unique column, and
   a rendering that is only unambiguous on a tidy database is not unambiguous.
2. **The notation needs a legend**, charged once per prompt at 49 tokens. A
   terse form nobody explained is a guessing game, and the legend is what lets
   the notation stay terse.

Those 86 tokens are most of the gap between the sketched −37% and the measured
−24%. The saving is real and worth having; it is simply two-thirds of what the
spec advertised, and the honest version of that sentence belongs here rather
than in a footnote after the A/B has run.

`005` Q-A chose DDL deliberately, on the grounds that it is the shape the model
has seen most and *"the resemblance to training data plausibly does"* matter at
this size. That was a reasonable bet, and it was never tested against accuracy.
Iteration 5 has an eval harness and can settle it — **and nothing above settles
it**, because every number here is a cost and none is a benefit.

```
album(album_id int, title str, artist_id int) PK[album_id] FK[artist_id->artist]
```

`005` Q-A chose DDL deliberately, on the grounds that it is the shape the model
has seen most and *"the resemblance to training data plausibly does"* matter at
this size. That was a reasonable bet, and it was never tested against accuracy.
Iteration 5 has an eval harness and can settle it.

### 2.3 The largest token lever is accuracy itself

Per-question cost is `calls × prompt size`. With the schema present the model
answers in one call and is right ~95–97% of the time, so a full-schema pass
costs ~41,000 tokens — barely more than single-shot's 37,600. The blind harness
costs up to ~98,000 because it *always* needs at least two calls.

So the token problem is not the budget being 3. **It is failure.** Every
question answered on the first attempt costs a third of one that needs three,
and the way to spend fewer tokens is to be right sooner. Compaction and
accuracy work the same direction; a smaller budget would not.

### 2.4 Chinook cannot express metric ambiguity, but it can express population ambiguity

Ten candidate business terms, each compared as *naive reading* against
*conventional reading*:

> **One row corrected at T3.** *Average order value* was published with its
> columns the wrong way round — per invoice under "naive", per line under
> "conventional". An `invoice_line` is not an order, and AOV universally means
> revenue per order, so the glossary defines the per-invoice figure as the
> convention and the per-line figure as the naive misreading. **Both measured
> numbers stand; only which one is which has flipped.** Defining it as first
> published would have taught a definition most analysts call wrong and built
> an expert question that punishes the correct instinct.
>
> Every other row was re-executed at T3 through `execute_sql()` and matched
> exactly.

| Term | Naive | Conventional | Discriminates |
|---|---|---|---|
| revenue: invoice total vs line items | 2328.60 | 2328.60 | **no** |
| average order value: per line vs per invoice | 1.0396 | 5.6519 | yes |
| catalogue tracks vs tracks ever sold | 3503 | 1984 | yes |
| all employees vs support representatives | 8 | 3 | yes |
| all customers vs active in final 12 months | 59 | 46 | yes |
| artists vs artists with a track ever sold | 275 | 165 | yes |
| genres vs genres with any sales | 25 | 24 | yes |
| playlists vs non-empty playlists | 18 | 14 | yes |
| albums vs albums with a track | 347 | 347 | **no** |
| all tracks vs tracks with a composer | 3503 | 2526 | yes |

**8 of 10 discriminate, and the two that fail are the classic ones.** The
textbook analytics trap — "what does revenue mean?" — is untestable here,
because Chinook's arithmetic is internally consistent: the invoice total and the
sum of its line items agree to the cent, by construction.

What *does* work is **population definition**: which rows count as a customer, a
track, an artist. That is a different kind of domain knowledge from metric
definition, and it happens to be the kind this schema supports.

One of those rows deserves note. *"All employees vs support representatives",*
8 against 3, is precisely the ambiguity that got `medium-008` retired in
Iteration 3 as unfair. With the convention stated in a glossary it stops being
unfair and becomes a legitimate hard question — the same trap, now with the
information needed to avoid it.

---

## 3. Acceptance criteria

### Removing `sample_rows`

- **AC1** — `sample_rows` is removed from the agent's offered actions and moves
  to `EXCLUDED_ACTIONS` with its measured justification. The prompt shrinks by
  **19 tokens** (corrected from 22 at T1, §2.1) and the model's choice space by
  one.
- **AC2** — Attempting it becomes an ordinary `unknown_action` observation, not
  an error — the recovery path `007` §2.5 already measured.
- **AC3** — `api/db/sampling.py` and its tests are **kept** (resolved Q-B).
  Deleting them buys zero tokens, and the capability is the only way the agent
  could ever inspect a value format. **Its `TOOLS` entry is not kept**: Q-B
  resolved to remove the surface, and T1 measured that the registry entry is
  load-bearing for that (see AC4a).
- **AC4** — Every `TOOLS` entry is offered to the model or explicitly excluded,
  so this is a reclassification rather than a hole. `EXCLUDED_ACTIONS` may now
  name a **retired** capability that `TOOLS` no longer contains, so this is a
  containment rule, not the set equality it was through Iteration 4.
- **AC4a** — **Every offered action can actually dispatch.** The converse of
  AC4, and the failure this change newly makes possible: an action described in
  the prompt whose implementation has been withdrawn would be chosen and then
  rejected, spending a call on a capability the prompt promised.
- **AC4b** — **No dispatch path may survive the withdrawal of a tool.**
  Measured at T1: removing only the `TOOLS` entry left `sample_rows` fully
  operational, because the orchestrator dispatched it through a direct import
  rather than through the registry — it read rows from the database with no
  registry entry present. Removal is complete only when no code path reaches
  the implementation.

### Token cost

- **AC5** — **The eval runner reports tokens consumed**, per run and per
  question. Iteration 4 hit a hard daily limit with no visibility at all and
  discovered it as 31 provider errors; a benchmark that cannot see its own
  costliest resource will hit it again.
- **AC6** — Schema rendering becomes selectable, and DDL versus compact is
  **measured against accuracy**, not chosen by argument. `005` Q-A's bet gets
  tested rather than inherited.
- **AC7** — A rendering change alters the prompt fingerprint, so no run can be
  compared across renderings by accident (`006` AC24, `007` D-1).
- **AC8** — The run **aborts before spending anything** if its projected token
  cost exceeds a configured ceiling. A run that dies at question 31 wastes the
  30 that succeeded and produces a number that is not a measurement.
- **AC9** — Rate-limit failures are **reported separately from model failures**
  in the breakdown. Iteration 4's 82.5% was 33 correct and 7 rate-limited, and
  reading that as accuracy would be wrong in both directions.

### Domain knowledge

- **AC10** — A **glossary block** in the system prompt defines the business
  terms the questions use, in terms of the schema.
- **AC11** — Glossary terms are **population definitions**, not metric
  definitions (§2.4). A term whose two readings return identical rows tests
  nothing.
- **AC12** — **Every domain question must be provably discriminating**: the
  naive gold and the conventional gold return *different* rows, asserted by a
  test. This is the direct analogue of `006`'s duplicate-row check — a question
  that scores correct under either reading is not measuring the glossary.
- **AC13** — Accuracy is reported **with and without the glossary**. The
  difference is the only evidence that the glossary does anything.

  > **NOT SATISFIED as of T7.** The glossary-off control arm was dropped from
  > the T7 matrix on 2026-09-04 so that D-2 could be settled inside one day's
  > quota, so this iteration currently reports accuracy **with** the glossary
  > only. The obligation is not discharged, and is recorded here rather than
  > carried in memory. The one data point that exists is a three-question
  > pre-flight probe in which `expert-001` was correct with the glossary and
  > `wrong_result` without it - a signal at n=1, not the measurement this
  > criterion asks for.

### The dataset extension

- **AC14** — New questions get **new ids** in a new `expert` tier. Nothing
  existing is edited (`006` AC2), and dataset version becomes 3.
- **AC15** — An `expert` question is hard because of **interpretation**, not
  syntax. The `hard` tier already covers constructs and scores 100%; more window
  functions would measure nothing new.
- **AC16** — **Ambiguity that has no single right answer stays banned.** `006`
  retired `medium-008` and `easy-010` for exactly that, and this iteration must
  not smuggle it back under the name "realistic". The test: given the glossary,
  a competent analyst produces one answer. Without it, they cannot.
- **AC17** — `MAX_QUESTIONS` is raised if 50 is reached; the current cap is
  exactly 50 and the corpus is 40.

### Guardrails

- **AC18** — **No prompt change is accepted on the strength of a single run.**
  `007` measured a spread of 0.0% across three passes at temperature 0, so a
  difference of one question is a real difference — but only when both runs
  completed without rate limiting (AC9).
- **AC19** — Prompt versions are recorded in `EVALS.md` by fingerprint, and a
  regression stays in the file (`006` AC25).
- **AC20** — The eval dataset is **not edited to make a prompt look better.**
  The standing rule from `evals/questions.yaml` applies unchanged, in both
  directions.

---

## 4. Non-goals

| Excluded | Why |
|---|---|
| Fine-tuning a model | Out of scope for this project entirely |
| Few-shot examples drawn from the eval set | Would be training on the test set, straightforwardly |
| Provider-side prompt caching | Vendor-specific, and would put a vendor concept behind the swappable interface |
| Raising the call budget | §2.3 — the lever is being right sooner, not trying more often |
| A second dataset | Q-C asks about it; it is not assumed here |
| Latency optimisation | Iteration 7 |

---

## 5. Contracts

```python
# api/agent/prompts.py
# Three, not two: resolved at T2, where all three compact fields (view
# marking, nullability, explicit FK targets) were kept and `compact-abbrev`
# was built alongside `compact`. This line still said two until T7.
SCHEMA_RENDERINGS = ("ddl", "compact", "compact-abbrev")

def render_schema(schema, rendering: str = "ddl") -> str: ...
def build_loop_system(schema, schema_mode, rendering="ddl", glossary=True) -> str: ...

GLOSSARY: dict[str, str]   # term -> definition in schema terms

# evals/scoring.py
@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    calls: int
```

```yaml
# evals/questions.yaml — the expert tier
- id: expert-001
  tier: expert
  question: How many active customers are there?
  gold_sql: SELECT count(DISTINCT customer_id) FROM invoice WHERE ...
  naive_sql: SELECT count(*) FROM customer      # AC12 — must differ from gold
  glossary: [active customer]
  ordered: false
  covers: [customer, invoice]
```

`naive_sql` is not scored. It exists so a test can prove the question
discriminates, and it is the mechanism that stops an `expert` question quietly
becoming a free point.

---

## 6. Verification

- **Discrimination is a test, not a claim** (AC12): every `expert` question's
  `naive_sql` and `gold_sql` execute and return different rows.
- Glossary terms are asserted to be **referenced** by at least one question, so
  the block cannot accumulate definitions nothing uses.
- Token accounting is tested against a fake provider reporting known counts.
- The abort-on-projection check (AC8) is tested by setting a ceiling of zero.
- **Mutation checks**: make `naive_sql` equal `gold_sql` and the discrimination
  test must go red; drop the glossary from the prompt and the with/without
  comparison must stop differing.

---

## 7. Open questions

- **Q-A — How do we tune a prompt without overfitting the benchmark?** **The
  most important question here, and it is not technical.**

  Iteration 5's whole activity is changing the prompt until the number goes up.
  That is the definition of fitting to the test set, and `006` exists to stop
  exactly this kind of self-deception. Three options:

  1. **Hold out a slice.** Split the corpus — tune against a `dev` subset,
     report the `test` subset untouched. Costs questions from the headline and
     is the standard answer.
  2. **Tune on the withheld-schema harness, report the full-schema one.** They
     are different enough that improvements have to generalise, and both already
     exist.
  3. **Accept it and disclose it**, recording in `EVALS.md` that Iteration 5's
     numbers are in-sample.

  *My lean: (1), with the split fixed and recorded before any tuning starts* —
  the same ordering discipline `006` used, for the same reason. (2) is
  attractive but the two harnesses share all 40 questions, so a question-level
  overfit shows up in both.

- **Q-B — Delete `api/db/sampling.py`, or only remove it from the agent?**
  Deleting the module removes ~370 lines and its tests and buys **zero** tokens
  (§2.1); the saving is entirely in the 22-token prompt description, which AC1
  captures either way. Keeping it costs nothing at runtime and preserves the
  only mechanism by which the agent could ever inspect a value's format —
  something the `expert` tier might well need.

  *My lean: keep the module, remove the surface.*

- **Q-C — Is Chinook enough?** §2.4 shows the classic metric ambiguity is
  untestable here because the data is arithmetically consistent, and 8 of 10
  workable terms are population definitions. That is real headroom but it is
  bounded, and "real-world messiness" — inconsistent codes, duplicate entities,
  NULLs that mean three different things — is largely absent by construction.

  *My lean: exhaust the population-definition seam first.* A second dataset is a
  large piece of work (new seed, new gold, new fingerprint) and this one is not
  yet spent.

- **Q-D — Should the glossary always be present, or only for `expert`
  questions?** Always-on is honest to a real deployment and costs tokens on all
  40 questions. Conditional is cheaper and makes AC13's with/without comparison
  trivial, but a glossary that appears only for questions needing it leaks which
  questions those are.

  *My lean: always on, measured both ways.* AC13's comparison is a measurement,
  not the shipping configuration.

- **Q-E — What happens if compact rendering wins on tokens but loses on
  accuracy?** Plausible: 54% fewer tokens for some accuracy is a trade nobody
  specified a rate for. *My lean: accuracy wins outright at this stage* — the
  project is not yet cost-constrained in production, only in a free-tier
  benchmark, and Iteration 7 owns cost.

  **Resolved 2026-09-03 as the plan's D-2 — pre-registered before T7 runs, and
  recorded here rather than in the plan so the criterion sits with the
  acceptance criteria it governs:**

  > **Adopt the most compact rendering whose dev-split accuracy is within one
  > question of DDL. Otherwise keep DDL.**

  One question is the smallest difference a 30-question dev split can resolve
  (plan §3), so a tolerance narrower than that would be deciding on noise, and
  one wider would trade accuracy the project has not priced. T7 applies the
  rule **mechanically** and reports the arithmetic — including, explicitly, a
  case where the rule adopts a rendering that scored lower than DDL.

  Writing the criterion down while the outcome is still unknown is the same
  ordering discipline `006` used when it wrote all 40 gold queries before the
  runner existed. The value is lost entirely if the rule is revisited once the
  numbers are visible.

  **The token half of this trade must be measured, not estimated** (§2.2). A
  rule that trades accuracy against tokens is meaningless if the token figure
  is `chars // 4`.

  > **APPLIED 2026-09-04 at T7: `compact` is adopted.** On the 30-question dev
  > split, two passes each: `compact` 100.0% (30/30, 30/30), `ddl` 98.3%
  > (29/30, 30/30). The delta is +0.5 questions a pass in `compact`'s favour,
  > inside the one-question tolerance, so the rule adopts the more compact
  > rendering. `ddl`'s single miss was `rate_limited` rather than wrong;
  > excluding it makes both arms 100% and the delta zero, which is the same
  > decision. **The trade this rule was written to arbitrate never arose** -
  > no accuracy was given up for the saving.
  >
  > `compact-abbrev` was withdrawn from the matrix before it ran (plan
  > section 8), so *most compact* here means most compact **of the two arms
  > tested**. That rendering still exists and is still tested; it has simply
  > never been scored, and this entry is not evidence against it.

---

## 8. What this unblocks

Iteration 6 puts this in front of users, where a wrong answer is worse than a
slow one. Iteration 7 owns latency and cost, and inherits AC5's token accounting
as the thing it optimises against.
