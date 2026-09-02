"""Evaluation suite — `specs/006-evals.md`.

A package rather than loose scripts so the tests can import the runner and score
a full pass against a fake provider, with no subprocess and no API key.

**Not shipped in the API image.** `evals/` is a measurement harness, and its one
extra dependency (`pyyaml`) lives in `api/requirements-dev.txt` for that reason.
"""
