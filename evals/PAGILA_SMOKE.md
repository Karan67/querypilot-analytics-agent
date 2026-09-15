# Pagila smoke results

Iteration 14 (`specs/017-schema-generality.md`), AC1/AC3/AC5. **Not**
`EVALS.md` -- these numbers are never blended with Chinook's.

Run: 2026-09-15T21:54:28+00:00

## Findings this run surfaced, not just its scores

**The "active customers" question is the concrete case for AC2's configurable
glossary.** Pagila's `customer` table carries two independent activity flags —
`active` (integer) and `activebool` (boolean) — and they disagree on 43 rows:
966 customers have `active = 1`, 987 have `activebool = true`, with 11 rows
where the first says active and the second doesn't and 32 the other way
(confirmed live, `docker exec querypilot-pagila-db psql`). Asked "how many
customers are active?" with no glossary configured, the model chose
`activebool` and produced 987 — a fluent, executable, wrong-by-omission
answer, not a crash. Chinook cannot produce this kind of finding: it has no
pair of columns that disagree about the same fact. This is exactly the
population-ambiguity shape `api/agent/glossary.py`'s existing eight terms
exist to pin down for Chinook, now with a second, independently-discovered
example of why the mechanism has to be configurable per database rather than
hardcoded to one domain's vocabulary (Iteration 14 T4 made it so:
`QUERYPILOT_GLOSSARY_FILE`, resolved Q-B). A Pagila deployment that cared
about this question would supply a glossary entry such as `"active customer":
"a customer with activebool = true"` (or `active = 1`, whichever the business
actually means) the same way Chinook's own file does.

**`payment`'s partitioned parent is invisible to introspection** — recorded as
`B-16` in `specs/000-project.md` §8, not fixed in this iteration. See the
question below for what the model did about it unprompted.

## How many films are in the catalog?

- Expected: one row, count = 1000
- `ok`: True
- SQL: `SELECT COUNT(*) AS film_count FROM film;`
- Columns: ('film_count',)
- Rows returned: 1
- Rows: ((1000,),)
- Attempts used: 1

## How many actors are there?

- Expected: one row, count = 200
- `ok`: True
- SQL: `SELECT COUNT(*) FROM actor;`
- Columns: ('count',)
- Rows returned: 1
- Rows: ((200,),)
- Attempts used: 1

## Which film has the longest length, and what is its rating?

- Expected: one row: GANGS PRIDE, PG-13, length 185
- `ok`: True
- SQL: `SELECT title, rating FROM film ORDER BY length DESC NULLS LAST LIMIT 1;`
- Columns: ('title', 'rating')
- Rows returned: 1
- Rows: (('GANGS PRIDE', 'PG-13'),)
- Attempts used: 1

## How many customers are active?

- Expected: one row, count = 966 (customer.active = 1)
- `ok`: True
- SQL: `SELECT COUNT(*) AS active_customers FROM customer WHERE activebool = true;`
- Columns: ('active_customers',)
- Rows returned: 1
- Rows: ((987,),)
- Attempts used: 1

## What is the total amount collected from all payments?

- Expected: deliberate stress case: payment is a partitioned table with 55 monthly children and no queryable parent relation, confirmed by direct introspection (api.db.introspection.get_schema() does not list 'payment', only 'payment_p*'). True total across every partition, summed by hand: 170962.39. Whether the agent notices it can only see partitions -- and how it responds -- is the finding this question exists to surface, not a pass/fail gate.
- `ok`: True
- SQL: `SELECT SUM(amount) AS total_amount FROM (
  SELECT amount FROM payment_p2022_01
  UNION ALL SELECT amount FROM payment_p2022_02
  UNION ALL SELECT amount FROM payment_p2022_03
  UNION ALL SELECT amount FROM payment_p2022_04
  UNION ALL SELECT amount FROM payment_p2022_05
  UNION ALL SELECT amount FROM payment_p2022_06
  UNION ALL SELECT amount FROM payment_p2022_07
  UNION ALL SELECT amount FROM payment_p2022_08
  UNION ALL SELECT amount FROM payment_p2022_09
  UNION ALL SELECT amount FROM payment_p2022_10
  UNION ALL SELECT amount FROM payment_p2022_11
  UNION ALL SELECT amount FROM payment_p2022_12
  UNION ALL SELECT amount FROM payment_p2023_01
  UNION ALL SELECT amount FROM payment_p2023_02
  UNION ALL SELECT amount FROM payment_p2023_03
  UNION ALL SELECT amount FROM payment_p2023_04
  UNION ALL SELECT amount FROM payment_p2023_05
  UNION ALL SELECT amount FROM payment_p2023_06
  UNION ALL SELECT amount FROM payment_p2023_07
  UNION ALL SELECT amount FROM payment_p2023_08
  UNION ALL SELECT amount FROM payment_p2023_09
  UNION ALL SELECT amount FROM payment_p2023_10
  UNION ALL SELECT amount FROM payment_p2023_11
  UNION ALL SELECT amount FROM payment_p2023_12
  UNION ALL SELECT amount FROM payment_p2024_01
  UNION ALL SELECT amount FROM payment_p2024_02
  UNION ALL SELECT amount FROM payment_p2024_03
  UNION ALL SELECT amount FROM payment_p2024_04
  UNION ALL SELECT amount FROM payment_p2024_05
  UNION ALL SELECT amount FROM payment_p2024_06
  UNION ALL SELECT amount FROM payment_p2024_07
  UNION ALL SELECT amount FROM payment_p2024_08
  UNION ALL SELECT amount FROM payment_p2024_09
  UNION ALL SELECT amount FROM payment_p2024_10
  UNION ALL SELECT amount FROM payment_p2024_11
  UNION ALL SELECT amount FROM payment_p2024_12
  UNION ALL SELECT amount FROM payment_p2025_01
  UNION ALL SELECT amount FROM payment_p2025_02
  UNION ALL SELECT amount FROM payment_p2025_03
  UNION ALL SELECT amount FROM payment_p2025_04
  UNION ALL SELECT amount FROM payment_p2025_05
  UNION ALL SELECT amount FROM payment_p2025_06
  UNION ALL SELECT amount FROM payment_p2025_07
  UNION ALL SELECT amount FROM payment_p2025_08
  UNION ALL SELECT amount FROM payment_p2025_09
  UNION ALL SELECT amount FROM payment_p2025_10
  UNION ALL SELECT amount FROM payment_p2025_11
  UNION ALL SELECT amount FROM payment_p2025_12
  UNION ALL SELECT amount FROM payment_p2026_01
  UNION ALL SELECT amount FROM payment_p2026_02
  UNION ALL SELECT amount FROM payment_p2026_03
  UNION ALL SELECT amount FROM payment_p2026_04
  UNION ALL SELECT amount FROM payment_p2026_05
  UNION ALL SELECT amount FROM payment_p2026_06
  UNION ALL SELECT amount FROM payment_p2026_07
) AS all_payments;`
- Columns: ('total_amount',)
- Rows returned: 1
- Rows: ((Decimal('170962.39'),),)
- Attempts used: 1

