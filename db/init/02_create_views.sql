-- Postgres init hook 2 of 3 - views over the sample dataset.
--
-- ORDERING IS LOAD-BEARING. This file must run after 01 (which creates the
-- tables this view depends on) and BEFORE 03 (which grants SELECT to the
-- read-only role). GRANT ... ON ALL TABLES is a one-time snapshot over
-- relations existing at grant time, not a standing rule - verified directly in
-- T1, where every relation created after the grant was denied to the role.
--
-- A view created after the grant would still be reported by get_schema(), so
-- the agent would write a correct query against a relation it cannot read. The
-- SQL would be valid, validation would pass, and execution would fail with
-- permission denied - a failure the agent cannot self-correct out of, because
-- nothing about its query is wrong.
--
-- 03 also installs ALTER DEFAULT PRIVILEGES as a second line of defence for
-- relations added later, but that is not retroactive, so this ordering still
-- carries the load for everything created here.

CREATE VIEW invoice_totals AS
SELECT i.invoice_id,
       i.customer_id,
       i.invoice_date,
       count(il.invoice_line_id)              AS line_count,
       sum(il.unit_price * il.quantity)       AS total
FROM invoice i
JOIN invoice_line il ON il.invoice_id = i.invoice_id
GROUP BY i.invoice_id, i.customer_id, i.invoice_date;

COMMENT ON VIEW invoice_totals IS
    'One row per invoice with line count and recomputed total. Exercises the '
    'view path in get_schema(): no primary key, no foreign keys, computed columns.';
