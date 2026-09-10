"""The schema, introspected once and reused until the catalog actually changes.

**AC9's second half is the whole difficulty.** Caching an introspection is
trivial; the criterion says the reuse must be *invalidated on a schema change
rather than trusted forever*, and a cache cannot notice a change it never looks
for. A time-to-live would not be invalidation — it would be trusting the schema
for sixty seconds and then paying full price whether anything moved or not.

So the schema is reused only while a **cheap probe says the catalog is
unchanged**, and the arithmetic is what makes that worth doing. Measured in the
container on 2026-09-10:

| | round trips | time |
|---|---|---|
| `get_schema()` | **52** | 99ms median |
| this probe | 3 | **5.3ms** median |

Fifty-two, because SQLAlchemy's `Inspector` asks per relation: columns, primary
key, foreign keys, and kind, for each of twelve relations. The probe asks once.

**The probe goes through `execute_sql()`,** so it passes Gate 2, runs in the
read-only transaction and inherits the statement timeout. Charter §4 is absolute
and has exactly one recorded exemption, which is not this. It reads `pg_catalog`
rather than `information_schema` because the latter's views are themselves
expensive joins, which would spend the saving on measuring it.

**A signature it cannot verify is never trusted.** Two cases return `None` and
force a real introspection: the probe failing, and the probe being *truncated*
by the Gate 3 row cap. The second is the one worth naming — 91 signature rows
here against a cap of 1,000, but a warehouse with two hundred tables would
exceed it, and a fingerprint computed over the first thousand rows would be
perfectly stable while blind to every change past them. That is a cache that
looks correct and silently is not.

**What the saving is, honestly.** `010-hardening.md` §2.4 already said it: 99ms
of a ~1,000ms request is about 10% of the fast mode and 1% of the slow mode, and
it saves **zero tokens**. It does not touch §2.3's constraint. What T4 added was
a second introspection per question, and this removes both.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable

from api.db.execution import execute_sql
from api.db.introspection import Schema, get_schema

#: Characters of the hash kept, matching `api/agent/fingerprints.py`.
FINGERPRINT_LENGTH = 12

#: One row per column and one per constraint, over every relation the schema
#: tool reports.
#:
#: **Coverage is the correctness property**, and it is asserted rather than
#: argued: `tests/test_schema_cache.py` walks everything `get_schema()` returns
#: and requires each table, column, type and constraint to appear in this
#: material. Anything the `Schema` carries but the signature omits is a change
#: the cache could not see -- an added foreign key, say, leaving the prompt
#: describing a relationship that no longer exists.
#:
#: `relkind` and `attnotnull` are cast explicitly: `text || "char"` is ambiguous
#: in PostgreSQL and fails at execution rather than at parse time, which is how
#: the first version of this query passed Gate 2 and then fell over.
CATALOG_SIGNATURE_SQL = """
SELECT c.relname || '|' || c.relkind::text || '|' || a.attname || '|'
       || format_type(a.atttypid, a.atttypmod) || '|' || a.attnotnull::text
       AS signature
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
 WHERE n.nspname = 'public'
   AND c.relkind IN ('r', 'v', 'm')
   AND a.attnum > 0
   AND NOT a.attisdropped
 UNION ALL
SELECT 'constraint|' || conrelid::regclass::text || '|' || conname || '|'
       || pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE connamespace = 'public'::regnamespace
"""

_lock = threading.Lock()
_schema: Schema | None = None
_fingerprint: str | None = None


def fingerprint_of(signatures: Iterable[str]) -> str:
    """Hash catalog signature rows.

    **Sorted here rather than in SQL.** `ORDER BY` would make the hash depend on
    the database's collation, so the same schema on a differently-configured
    server would fingerprint differently -- and the ordering of rows is not a
    property of the schema in any case. Sorting in Python makes this a function
    of the *set* of signatures, which is what a schema is.

    Separated from the query so it can be tested without a database, which is
    the only way to assert what the hash is sensitive to.
    """
    digest = hashlib.sha256()
    for signature in sorted(str(s) for s in signatures):
        digest.update(signature.encode("utf-8"))
        # A separator, for the reason the cache key has one: without it, two
        # adjacent signatures could be re-split differently and hash the same.
        digest.update(b"\x00")
    return digest.hexdigest()[:FINGERPRINT_LENGTH]


def catalog_fingerprint() -> str | None:
    """What the catalog looks like right now, or `None` if that cannot be told.

    `None` is not an error and not an empty schema — it means *unverifiable*,
    and every caller treats it as a reason to introspect for real rather than as
    a value to compare. Returning a fingerprint here on a failed or truncated
    probe would be the cache's worst failure mode: confidently stable, and
    wrong.
    """
    result = execute_sql(CATALOG_SIGNATURE_SQL)

    if not result.ok:
        return None

    if result.truncated:
        # Gate 3 capped the signature. The rows that came back are a prefix, so
        # a hash of them is blind to everything after it -- and blind in a way
        # that never looks broken, because a prefix of a stable catalog is
        # itself perfectly stable.
        return None

    return fingerprint_of(row[0] for row in result.rows)


def cached_schema() -> Schema:
    """`get_schema()`, reused while the catalog is unchanged (AC9).

    Raises `SchemaIntrospectionError` exactly as `get_schema()` does, so callers
    that already handle an unreachable database keep working unchanged.
    """
    global _schema, _fingerprint

    fingerprint = catalog_fingerprint()

    with _lock:
        if fingerprint is not None and fingerprint == _fingerprint and _schema is not None:
            return _schema

    # **Outside the lock deliberately.** This is 52 round trips and ~99ms, and
    # holding a lock across it would serialise every request behind the first
    # one after a schema change. Two threads racing here both introspect and
    # both store the same answer, which costs one redundant read and no
    # correctness -- the trade a request path should make.
    schema = get_schema()

    with _lock:
        _schema = schema
        # Stores `None` when the probe could not be verified, so the next call
        # cannot match it and will introspect again. An unverifiable catalog is
        # never cached.
        _fingerprint = fingerprint

    return schema


def clear() -> None:
    """Forget the cached schema. For tests, and for the same reason every other
    module in this iteration grew one: shared state a test can reach will
    eventually be written by one."""
    global _schema, _fingerprint
    with _lock:
        _schema = None
        _fingerprint = None
