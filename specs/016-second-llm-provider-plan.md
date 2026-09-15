# 016 — Iteration 13 plan: Second LLM provider (Cerebras)

## 1. Approach

Mirror the existing `groq_provider.py` shape exactly: one module, one vendor
client, mapped onto the same `LLMProvider` Protocol, the same `LLMError` /
`RateLimitError` exception types, the same `config.py` secret pattern. The
factory gets a second `if` branch. Nothing above `api/llm/` changes, per the
provider-boundary contract in CLAUDE.md.

Two things this iteration also has to fix rather than extend, because
extending them would repeat a documented defect shape: the vendor-import
structural test (currently two hardcoded strings waiting to become three),
and `EVALS.md`'s missing `Provider` field (currently ambiguous the moment
two providers can serve a model literally named the same thing — see §4,
which is not hypothetical: both providers can serve a `gpt-oss-120b`).

## 2. `api/llm/cerebras_provider.py`

- Constructor takes `api_key: str`, `model: str`, fails fast (raises
  `LLMError`) if the key is empty — identical shape to `GroqProvider.__init__`
  (`api/llm/groq_provider.py:44-60`).
- `complete(system, user)`: clears `_last_usage` / `_last_rate_limit` at
  entry (matching Groq's reset-per-call behavior), issues the chat-completion
  call, catches the SDK's rate-limit exception → `RateLimitError` (reading
  whatever rate-limit snapshot is available first), catches broad
  `Exception` → `LLMError`, both messages passed through a `_safe_message`
  equivalent that scrubs the API key by literal substitution, same as
  Groq's.
- `last_usage` / `last_rate_limit`: best-effort `getattr`-style attributes,
  populated from whatever the response actually contains. Per D-F, if the
  usage object is partial or absent (unverified until §7's billing risk
  clears — see spec §2.6), these return partial data or `None` rather than
  raising.
- API key: `config.get_secret("CEREBRAS_API_KEY")`, wired into
  `factory.py`'s new `"cerebras"` branch alongside a
  `QUERYPILOT_LLM_MODEL` override, same pattern as Groq's branch.

## 3. `api/llm/rate_limits.py` — Cerebras-specific additions (D-D)

Add Cerebras-specific header-name constants and a `limit_from_message`-style
parser as their own functions alongside the existing Groq-specific ones in
the same module (not a new file — the module already exists to hold
provider-specific rate-limit parsing, and D-D decided against generalizing
`evals/ledger.py` this iteration). Every header name pulled from spec §2.4
is doc-sourced from a third-party page, so the parser must treat every
header as optional and fall back to no-snapshot rather than raising or
assuming a header is present. This gets re-verified against a real 429 once
the billing blocker (spec §7) clears — that re-verification is its own task
in §6, not assumed done here.

## 4. `EVALS.md` schema (D-C)

Add a `Provider` row to the entry template (next to the existing `Model`
row). Note the schema addition once in `EVALS.md`'s own preamble, dated,
rather than backfilling old entries — they stay as they are, since the file
is append-only. The two new Cerebras entries (dev-split single-shot and
loop, per D-A) use the new field; every prior entry implicitly reads as
Groq, which was already unambiguous before Cerebras existed.

## 5. Vendor-import structural test (D-B)

Replace the string-prefix scan in
`test_ac2_no_vendor_sdk_outside_the_llm_package`
(`tests/test_single_shot.py:262-280`) with real `ast.walk`-based import
detection: parse each non-`api/llm/` file's AST, collect every `Import` and
`ImportFrom` node's top-level module name, and assert none of them appear in
an explicit vendor allow-list (`{"groq", "cerebras"}` to start — named as a
set specifically so a third provider is one entry, not a new hardcoded
string). This directly follows this repo's own documented rule that a
"string must not appear" test has to parse the AST, not scan text — the
existing test already violated that rule for one vendor; this iteration
fixes it while it's already being touched rather than compounding it.

**Mutation check**: temporarily add `import groq` (or `import cerebras`) to
a file outside `api/llm/`, confirm the test goes red, then confirm it goes
green again after reverting — proving the AST walk actually inspects
imports rather than, say, only checking file paths.

## 6. Files and decomposition

| # | Task | Files | Depends on |
|---|---|---|---|
| T1 | `cerebras_provider.py` + Cerebras-specific `rate_limits.py` additions, built and unit-tested against **mocked** responses (billing-blocker-safe) | `api/llm/cerebras_provider.py`, `api/llm/rate_limits.py`, `tests/test_llm_provider.py` (extend, don't replace, the Protocol-conformance test) | — |
| T2 | `factory.py` branch + `config.py` secret wiring for `CEREBRAS_API_KEY` | `api/llm/factory.py` | T1 |
| T3 | Fix the vendor-import structural test (§5) + its mutation check | `tests/test_single_shot.py` | — (independent of T1/T2) |
| T4 | `EVALS.md` schema note + template `Provider` field | `EVALS.md` | — (independent) |
| T5 | **Blocked on billing** (spec §7): live smoke test, one dev-split single-shot + one loop `EVALS.md` entry against Cerebras, re-verification of spec §2.3/§2.4's doc-sourced numbers against a real response/429 | `tests/test_llm_live.py`, `EVALS.md` | T1, T2, T4, and the user adding a payment method to the Cerebras account |

T1–T4 have no live-billing dependency and can proceed now. T5 is the
acceptance-criteria item that cannot be honestly marked done — nor can this
iteration be reported as "proven" — until billing is resolved; per the
standing "report faithfully" rule, if T5 stays blocked, the final report
says so plainly rather than treating T1–T4 as sufficient.

## 7. Decisions

Two design questions surfaced while writing this plan that the spec did not
resolve — per the standing rule that decisions the plan itself surfaces are
still presented with a lean rather than decided silently, even under the
batched-rulings precedent (that precedent covers the spec's own open
questions, which arrived pre-answered; it does not cover new ones the plan
raises).

- **D-G — Official `cerebras-cloud-sdk` client, or raw HTTP via `requests`?**
  The measurement probe in spec §2.1 used raw `requests` for convenience,
  but that's not how `groq_provider.py` is built — it wraps the official
  `groq.Groq(...)` SDK client (`api/llm/groq_provider.py:60`), and reads
  rate-limit data off the SDK's own exception/response objects rather than
  parsing raw `requests.Response` headers by hand.

  *My lean: adopt the official `cerebras-cloud-sdk` package (confirmed to
  exist on PyPI/GitHub as `Cerebras/cerebras-cloud-sdk-python`), imported
  only inside `cerebras_provider.py`.* This matches the established
  one-SDK-per-provider-module pattern exactly, gets header/error parsing
  for free instead of hand-rolling it, and keeps `api/llm/`'s internal shape
  consistent rather than introducing a second, ad hoc style the day a third
  provider is added. The cost is one new runtime dependency
  (`cerebras-cloud-sdk`), scoped to a single file, same as `groq` is today.

- **D-H — Which of the two available models is the `factory.py` default?**
  §2.2 measured two live model ids: `qwen-3.8-27b` and `gpt-oss-120b`.
  Nothing in this account's response distinguishes them by capability.

  *My lean: `gpt-oss-120b`.* `EVALS.md`'s existing entries already record
  Groq running `openai/gpt-oss-120b` — using the same underlying open model
  on Cerebras isolates the comparison to what this iteration is actually
  about (infrastructure, rate limits, latency) rather than confounding it
  with a model-quality difference, which directly serves D-A's re-baselining
  runs. `QUERYPILOT_LLM_MODEL` still overrides it if the user wants
  `qwen-3.8-27b` instead.

## 8. Risks

Carried forward from the spec, with the implementation-specific edge:

- T5 cannot be completed or verified until the Cerebras account has billing
  enabled. T1–T4 proceed without it, built against mocked responses, but
  none of spec §3's acceptance criteria that require a real round trip can
  be marked done until then.
- D-G's choice of the official SDK means this iteration adds a new
  dependency (`cerebras-cloud-sdk`) to whatever manifest tracks Python
  dependencies for the API image — that file needs updating as part of T1,
  and the Docker image rebuilt/verified to still build per Iteration 12's
  deployment artifact constraints (non-root, multi-stage) before this is
  called done.
