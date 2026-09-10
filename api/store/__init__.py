"""Operational state: what was asked, what it cost, how long it took.

**This is not the analytics database and must never become it.** Charter §5
committed to the separation long before anything implemented it -- *"the target
database is read-only to the agent; history is written to the metadata store,
never back into the database being analysed"* -- and Iteration 7 is where that
commitment acquires an address.

The address is SQLite in a named volume. The consequence that matters is that
`api/main.py` still holds exactly one PostgreSQL credential and it is still
`querypilot_ro`, so charter §4 stays literally true rather than earning a second
recorded exemption (`010-hardening.md` Q-A).

`sqlite3` is in the standard library, so none of this adds a dependency to
either requirements file.
"""
