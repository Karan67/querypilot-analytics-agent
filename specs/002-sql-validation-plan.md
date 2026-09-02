# 002 — SQL Validation: Technical Plan

> **STATUS: AWAITING YOUR APPROVAL.** No application code exists against this.
> Spec: [`002-sql-validation.md`](002-sql-validation.md).
> §2 is the design and the only part with a real decision left in it. §6 asks
> you for two things.

---

## 1. Approach

`sqlglot.parse(sql, dialect="postgres")`, then three checks in increasing cost
order: length, statement count, and a structural pass over the tree.

The whole module is a pure function of a string. It opens no connection, so its
tests need no fixture and belong in CI from day one — a deliberate contrast with
`001`, where every test needed a live catalog.

**Everything below was measured against sqlglot 27.29.0 while drafting the
spec**, not inferred. The facts table in [`002-sql-validation.md` §5](002-sql-validation.md)
is the reference; this plan does not re-derive it.

---

## 2. The design

Four gates inside the function, cheapest first, each with a distinct reason
string:

```
length  →  parse  →  statement count  →  root allow-list  →  tree deny-list
 AC27       AC5          AC3, AC4         AC1, AC25, AC26    AC6-AC11, AC14, AC15
```

### Gate A — root allow-list (primary)

The root must be `exp.Select` or `exp.SetOperation`. Two entries, because
`Union`, `Intersect` and `Except` all subclass `SetOperation` — so this covers
set operations sqlglot adds later without an edit.

This alone rejects `DROP`, `ALTER`, `TRUNCATE`, `SET`, `GRANT`, `COPY`,
`VACUUM`, `DO` and `EXPLAIN` when they appear as the whole statement, because
none of them parse to an allowed root.

### Gate B — tree deny-list (catches what hides inside an allowed root)

Walk every node; reject on any forbidden type. This is what catches AC10 — the
data-modifying CTE whose root is a perfectly innocent `Select`.

**The deny-list must be explicit and enumerated.** The obvious implementation —
`isinstance(node, (exp.DML, exp.DDL, exp.Command))` — is wrong, and the spec
records why: `exp.Drop`, `exp.Alter`, `exp.TruncateTable`, `exp.Set` and
`exp.Grant` belong to none of those families. Gate A happens to catch those five
today, so relying on base classes would degrade defence in depth rather than
open a hole — but it would be a latent hole the moment Gate A is widened.

Proposed forbidden set, to be pinned in the module:

```
exp.DML, exp.DDL, exp.Command          # Insert, Update, Delete, Merge, Copy, Create, …
exp.Drop, exp.Alter, exp.TruncateTable # not in any base family
exp.Set, exp.Grant                     # nor these
exp.Into                               # SELECT … INTO (AC11)
```

The base classes stay in the set — they are correct as far as they go, and they
carry future subclasses. The explicit entries are there because they must be.

### Gate C — recursive branch validation (AC25)

Your Q-B answer. A set operation's branches are each validated as a query in
their own right, recursively, so the allow-list applies at every level rather
than only at the root.

Gate B's walk already descends into branches — verified: a `DELETE` CTE inside a
`UNION` arm is found. So Gate C is **belt-and-braces against a specific failure
mode**: if the walk is ever narrowed, or sqlglot changes what `walk()` traverses,
the branch check still holds the line. Worth its handful of lines on the
module the project's safety claims rest on.

### What I am *not* proposing

- No regex, anywhere, for any purpose.
- No function-name blocklist (`pg_sleep`, `pg_read_file`) — spec §4 non-goal.
- No rewriting, no `LIMIT` injection — your Q-A answer.
- No catching `Exception` broadly to satisfy AC22 without understanding what is
  thrown. See the risk in §7.

---

## 3. Files

| File | Status | Contents |
|---|---|---|
| `api/safety/validator.py` | exists, empty | `validate_sql()`, the allow/deny sets, reason constants |
| `api/agent/tools.py` | edit | Register `validate_sql` in `TOOLS`. Thin wrapper, per [`000-project.md` §5](000-project.md) |
| `api/requirements.txt` | edit | Add `sqlglot`. Currently only *mentioned in a comment* as a future dependency |
| `api/requirements-dev.txt` | no change | It already resolves `sqlglot` transitively via nothing — the runtime file is the right home, since the validator ships in the API |
| `tests/test_validator.py` | **new** | Table-driven must-reject / must-accept corpora |
| `tests/test_validator_gates.py` | **new** | The Gate 1 cross-check — the only test here that touches a database |
| `tests/test_tools.py` | edit | Registry now has two entries |

`sqlglot` is currently installed in `.venv` from spec research only. Pinning:
`sqlglot>=27,<28`. It is pre-1.0 and moves fast, and AC24 exists precisely
because its taxonomy is not a stable contract.

---

## 4. Reason strings (AC21)

Your Q-E answer was full detail, because the agent reads these and retries on
them. Each rejection path gets its own message naming what was found:

| Trigger | Reason |
|---|---|
| over length | `Query is 12,431 characters; the limit is 10,000.` |
| parse failure | `Could not parse as PostgreSQL: <sqlglot message>.` |
| >1 statement | `Expected exactly one statement, found 2 (Select, Drop). Only a single SELECT may be executed.` |
| bad root | `Statement type DELETE is not permitted. Only SELECT queries (optionally combined with UNION / INTERSECT / EXCEPT) may be executed.` |
| forbidden node | `Found a DELETE inside the query. Data-modifying statements are not permitted anywhere, including inside CTEs.` |

That last one matters: the model's most likely next move after
`WITH d AS (DELETE …)` is to try `WITH d AS (UPDATE …)`. The reason has to say
*anywhere, including inside CTEs*, or the retry loop burns attempts rediscovering
the rule.

---

## 5. Verification

**Table-driven corpora.** Two module-level lists — `MUST_REJECT` (input, expected
reason fragment) and `MUST_ACCEPT` — driven with `pytest.mark.parametrize` so a
new attack is one line, not one function.

**Every must-reject case asserts on the reason fragment, not just `ok is False`.**
A query rejected for the wrong reason is a test that keeps passing after the
logic that mattered is deleted.

**Mutation checks on the load-bearing gates**, the same discipline that proved
AC7 in `001`:

| Mutation | Expected |
|---|---|
| Remove Gate B's tree walk | AC10 cases fail, **and nothing else does** |
| Replace the explicit deny-list with the base classes alone | `DROP` / `SET` / `GRANT` node-level cases fail |
| Narrow the root allow-list to `exp.Select` only | AC25 set-operation cases fail |
| Remove the length check | AC27 fails |

If deleting a defence does not turn a test red, that test is decorative and gets
rewritten.

**Fuzz-ish cases for AC22**: random bytes, 10k nested parentheses, a
multi-megabyte string, lone surrogates. All must return `(False, reason)`, none
may raise.

**Gate 1 cross-check** (`tests/test_validator_gates.py`): for a sample of
`MUST_REJECT`, assert `querypilot_ro` is *also* refused by Postgres. Already
demonstrated by hand during spec drafting — the delete-CTE returns
`permission denied for table track` — but it belongs in the suite so the gates
stay independent rather than one quietly carrying the other.

---

## 6. Decisions I need from you

**D-1 — Does `validate_sql()` go in `TOOLS` now, or at Iteration 4?**
Registering it makes the tool surface complete and matches the §5 convention.
Against: nothing calls it through the registry until the agent loop exists, and
`get_schema` is in `TOOLS` only because it is the pattern-setter.
*My lean: register it.* The registry is documentation for Iterations 1–3, and a
tool missing from it is a worse lie than one nothing calls yet.

**D-2 — How aggressive should the AC24 drift test be?**
(i) Assert the 29 `STATEMENT_PARSERS` token names exactly match a frozen set —
any sqlglot change fails the build, including harmless additions like a new
dialect-specific verb. (ii) Assert only that no *unclassified* token can produce
an accepted parse. (i) is noisier but impossible to ignore; (ii) is quieter but
needs the classification logic to be right to be meaningful.
*My lean: (i).* This module is where a silent regression is least acceptable,
and a failing test after a deliberate dependency bump is a two-minute triage.

---

## 6b. Task log

| Task | Status | Outcome |
|---|---|---|
| **T1** | done | `sqlglot>=27,<28` added to `api/requirements.txt`, image rebuilt. Host and container both resolve 27.29.0 with 29 `STATEMENT_PARSERS` entries — version parity confirmed, which AC24 depends on |
| **T2** | done | Length, parse and statement-count gates. 39 validator tests |
| **T3+T4** | done | Landed **together**, deliberately — see below. Root allow-list + tree deny-list. 117 tests passing |
| **T5** | done | Recursive branch validation (AC25, AC26). 136 tests passing |
| **T6** | done | Reason quality. Two leaks fixed: bare expressions reported as "Statement type LITERAL is not permitted", and `<class 'sqlglot.expressions.Add'>` appearing in parse errors |
| **T7** | done | Hostile input. The `RecursionError` branch is **reachable and load-bearing** — 200 nested parens is ~410 characters, so the length cap gives no protection against depth |
| **T8** | done | All 29 statement tokens frozen (D-2). `SELECT` and `UNION` are not statement entry points at all, so the allow-list excludes every frozen token by construction |
| **T9** | done | Gate independence + parser-differential regression tests |
| **T10** | done | `validate_sql` registered in `TOOLS` (D-1 as assumed) |

**Iteration 1's validator is complete: 201 tests passing, all 27 acceptance
criteria covered.**

### Findings from T6–T10

**U+2028 is not a bypass, and proving that required the database.** A hostile
input test asserted `SELECT 1 -- ; DROP TABLE t` would be rejected, and it
was accepted. Python's `str.splitlines()` treats U+2028 as a line break, so it
looks like it should end a `--` comment. Measured across ten candidate
characters: PostgreSQL and sqlglot agree exactly — only LF and CR terminate a
comment, for both. Accepting it is correct, and rejecting it would have been
over-blocking. Now pinned by
`test_sqlglot_and_postgres_agree_on_comment_termination`, because a version bump
on *either* side could turn this into a real bypass.

**Gate 1 refuses two different ways.** A write gives "permission denied"; DDL on
another role's object gives "must be owner". A test asserting only the first
would silently stop covering `DROP`.

**`GRANT ALL ON track TO PUBLIC` does not error at all.** PostgreSQL warns and
grants nothing. Gate 1's guarantee is about **effect, not about raising** — so
the test verifies `has_table_privilege` afterwards rather than checking for an
exception, which is the property that actually matters.

**The seam between the gates is now a test.** `SET statement_timeout = 0` is
allowed by PostgreSQL for any role, so Gate 1 does *not* back up Gate 2 there.
`test_gate_one_does_not_cover_everything_gate_two_does` asserts both halves, so
the dependency recorded in [`000-project.md` §4](000-project.md) fails loudly if
AC14 is ever relaxed.

**A recorded exemption was required.** T9 executes forbidden SQL directly, which
the §4 standing rule forbids. Written into `000-project.md` §4 rather than left
in a test docstring.

### Findings from T5

**All 11 AC25/AC26 behavioural cases already passed before T5 was written.** The
root allow-list admits `SetOperation` and `walk()` already descends into
branches, so smuggled writes in a `UNION` arm — including two levels down — were
caught by T4. T5 adds the structural guarantee you asked for in Q-B, not the
rejection itself. Recorded plainly so the task log does not imply otherwise.

**Naive branch inspection would have over-blocked.** The obvious reading of
"each branch must itself be a query" is `(exp.Select, exp.SetOperation)`.
Measured, the branches of legitimate read-only set operations are wider::

    SELECT 1 UNION SELECT 2           -> Select,   Select
    (SELECT 1) UNION (SELECT 2)       -> Subquery, Subquery
    SELECT 1 UNION SELECT 2 UNION …   -> Union,    Select
    SELECT 1 UNION VALUES (1)         -> Select,   Values

Restricting to the narrow pair rejects parenthesised branches, nested set
operations, and `VALUES` — all read-only, all legitimate. `ALLOWED_BRANCHES` is
the wider measured set, and mutation-narrowing it fails 6 tests.

**The backstop's call site was untested.** A mutation removing
`_first_disallowed_branch(...)` from `validate_sql` — leaving the function and
its unit tests intact — **failed nothing**, because the tree walk reaches every
realistic payload first. The function was covered; its invocation was not.
Closed with `test_validate_sql_actually_consults_the_branch_scanner`, which
narrows the allow-list at runtime so the scanner is the only thing that can
reject the query. Re-running the mutation now fails.

**T3 and T4 were merged.** The decomposition assigned AC10 to T4, which means
T3 on its own would have produced a validator that accepts
`WITH d AS (DELETE …) SELECT * FROM d` — its root is a `Select`, so the
allow-list admits it. On a project whose central claim is blocking 100% of
DDL/DML, that state should not exist in the repo even briefly, so the two tasks
shipped as one change.

### Findings from T3+T4

**`REVOKE` is a seventh type missing from the base families.** The spec listed
five (`Drop`, `Alter`, `TruncateTable`, `Set`, `Grant`); `exp.Revoke` is another,
found while writing the corpus. Exactly the drift AC24 exists to catch, arriving
before AC24 was even built.

**`exp.Command` carries the leading keyword in `this`.** `SET ROLE`, `VACUUM`,
`DO`, `CALL` and `EXPLAIN` all fall back to `Command`, so the reason initially
read "an unsupported statement" for all five. Reading `this` lets it say `SET`
or `EXPLAIN` instead — materially better for AC21, since the agent retries on
that text.

**Mutation results.**

| Mutation | Result |
|---|---|
| Tree walk removed | 5 failures, all AC10/AC11, **nothing else** — precisely load-bearing |
| Deny-list replaced by base classes alone | **Only 1 failure.** The enumeration was effectively untested |

That second result is worth keeping. `Drop`, `Set`, `Grant`, `Revoke`, `Alter`
and `TruncateTable` cannot legally nest inside a `SELECT`, so the root allow-list
catches every one of them first and **no SQL input can distinguish the two
implementations**. Someone "simplifying" the deny-list to three base classes
would have seen a green suite while removing the defence that matters the moment
the allow-list widens.

Fixed by pinning the enumeration *structurally* rather than behaviourally:
`test_deny_list_enumerates_types_the_base_classes_miss` asserts each type is
present **and** still outside the base families. Re-running the mutation now
fails 2 tests instead of 1.

**D-1 answered by assumption:** `validate_sql` will be registered in `TOOLS` at
T10, per the plan's lean. Reversible; say otherwise and it waits for Iteration 4.

**D-2 resolved: freeze all 29.** AC24's test pins the full
`STATEMENT_PARSERS` token set, so any sqlglot change fails the build and forces
a human to classify the addition.

### Two findings from T2 that changed the implementation

**Empty statements are parsed, not skipped.** `sqlglot.parse` yields `None` for
an empty statement, so `""` comes back as one entry and `SELECT 1;;` as *two*.
Counting the raw list would have rejected a harmless trailing semicolon as a
stacked payload. `None` entries are filtered before counting — AC2 and AC4 both
depend on this and it is not obvious from the API.

**`TokenError` is a sibling of `ParseError`, not a subclass.** The plan said to
catch `ParseError` explicitly. That is wrong: the *tokenizer* raises
`TokenError` for input like `'unterminated`, which escaped as an exception and
broke AC22. Now catching `SqlglotError`, the common base.

Worth noting this is the **opposite** conclusion to §2's warning about the
expression taxonomy. There, base classes were insufficient and the deny-list had
to be enumerated. Here the base class is exactly right — every error sqlglot
raises while parsing means the same thing to us. The distinction: the deny-list
is a *security* classification where a missing member is a hole, whereas the
error handler is a *failure* classification where the answer is always "refuse".

The AC22 hostile-input corpus found this, not the documentation.

---

## 7. Risks

| Risk | Handling |
|---|---|
| **AC22 tempts a bare `except Exception`** — which would swallow real bugs and report them as invalid SQL | Catch `sqlglot.ParseError` and `RecursionError` explicitly; let anything else propagate during development, and revisit only with a named exception and a test that reproduces it |
| sqlglot taxonomy drifts on upgrade | AC24, pinned `>=27,<28` |
| The deny-list is enumerated, so something can be missing from it | Gate A's allow-list is the primary defence and is closed by construction; Gate 1 is independent. A deny-list miss degrades depth, it does not open the door |
| Over-blocking silently costs eval accuracy | AC18/AC19. Once Iteration 3 exists, every eval query runs through this gate as a test |
| `MUST_ACCEPT` corpus is invented by me, not drawn from real model output | Real generated SQL only exists from Iteration 2. Revisit the corpus then — noted here so it is not forgotten |
| Reason strings become a spec of their own | Assert on *fragments*, not whole strings, so wording can improve without churning tests |

---

## 8. Proposed decomposition

One task, one testable behaviour. I stop after each.

| # | Task | Verified by |
|---|---|---|
| **T1** | Add `sqlglot` to `api/requirements.txt`; rebuild the image; confirm it imports in the container | Container import check |
| **T2** | Length, parse, and statement-count gates | AC3, AC4, AC5, AC27 |
| **T3** | Root allow-list | AC1, AC2, AC6–AC9 (statement-level), AC13, AC14, AC15 |
| **T4** | Tree deny-list + explicit forbidden set | **AC10**, AC11, AC12 — plus the mutation check |
| **T5** | Recursive branch validation | AC25, AC26 |
| **T6** | Reason strings | AC21, and the reason-fragment assertions across the corpus |
| **T7** | Hostile-input hardening | AC22, AC23 |
| **T8** | AC24 drift test | Fails when the frozen token set is edited |
| **T9** | Gate 1 cross-check | `querypilot_ro` refuses a sample of `MUST_REJECT` |
| **T10** | Register in `TOOLS` (if D-1 says now) | Registry has two entries; wrapper delegates |

T4 before T5 so a failure in branch recursion cannot be mistaken for a failure
in the walk. T6 after T3–T5 because the reasons describe gates that must already
exist.
