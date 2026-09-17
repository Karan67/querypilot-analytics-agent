"""Iteration 15 — the Neon seeding runbook (`019-production-deployment.md` AC6-AC8).

Hermetic: reads `deploy/neon/README.md` as text. No database, no network.

The file is prose instructions for a human operator, not data a program
consumes, so this checks presence of the load-bearing names rather than
parsing structure the way `test_render_blueprint.py` does for `render.yaml`.
Whitespace is collapsed before any substring check, per the standing trap
where an assertion missed a phrase split across a line break.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNBOOK_PATH = REPO_ROOT / "deploy" / "neon" / "README.md"


def collapsed() -> str:
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", text)


# --- AC6: both databases and their seed scripts are named -------------------


def test_ac6_both_databases_are_named():
    text = collapsed()
    assert "chinook" in text
    assert "pagila" in text


def test_ac6_the_real_seed_scripts_are_named_not_paraphrased():
    text = collapsed()
    for name in (
        "db/fetch_chinook.sh",
        "db/fetch_pagila.sh",
        "db/init/02_create_views.sql",
    ):
        assert name in text, f"runbook does not name {name}"


# --- AC7: the manual, non-idempotent nature is disclosed --------------------


def test_ac7_the_one_time_manual_nature_is_disclosed():
    text = collapsed().lower()
    assert "one-time" in text or "one time" in text
    assert "manual" in text


# --- AC8: both target env vars are named, mapped to their database ----------


def test_ac8_both_render_env_vars_are_named():
    text = collapsed()
    assert "QUERYPILOT_DATABASE_URL" in text
    assert "QUERYPILOT_PAGILA_DATABASE_URL" in text


def test_ac8_the_read_only_role_is_named():
    """Gate 1 (specs/000-project.md §4): the deployed API must hold only the
    read-only role's credential, never the project superuser's — the runbook
    has to say so, not just show a CREATE ROLE statement with no context."""
    text = collapsed().lower()
    assert "read-only role" in text
    assert "querypilot_ro" in text
