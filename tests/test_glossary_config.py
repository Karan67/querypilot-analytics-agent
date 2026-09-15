"""Configurable glossary tests — `specs/017-schema-generality.md` AC2, D-1, D-2.

Two properties, and neither is about wording:

1. **Fails open (D-2).** Missing, unreadable or malformed
   `QUERYPILOT_GLOSSARY_FILE` never raises and never blocks `/ask` — it costs
   domain guidance, not availability, unlike `api/http/auth.py`'s credential
   map.
2. **An empty resolved glossary injects nothing (D-1).** Not an empty header
   over zero definitions — the same prompt shape as `glossary=False`.

No database needed: every path here is pure file I/O and string assembly.
"""

from __future__ import annotations

import json
import logging

import pytest

from api.agent.glossary import (
    GLOSSARY_FILE_ENV,
    _load_glossary_terms,
    current_glossary_terms,
    reset_glossary_cache,
)
from api.agent.prompts import SCHEMA_DDL, SCHEMA_FULL, build_loop_system
from api.db.introspection import Schema


@pytest.fixture(autouse=True)
def _reset_memo():
    """Every test starts from a clean memo, same discipline `auth`'s tests use."""
    reset_glossary_cache()
    yield
    reset_glossary_cache()


EMPTY_SCHEMA = Schema(tables=())


# --- _load_glossary_terms: the fail-open file reader -------------------------


def test_an_empty_path_is_no_glossary():
    assert _load_glossary_terms("") == {}
    assert _load_glossary_terms("   ") == {}


def test_a_missing_file_fails_open(tmp_path, caplog):
    missing = tmp_path / "does-not-exist.json"
    with caplog.at_level(logging.ERROR):
        result = _load_glossary_terms(str(missing))
    assert result == {}
    # Logged by config.read_secret_file, not re-logged here.
    assert any("does not exist" in r.message for r in caplog.records)


def test_malformed_json_fails_open_and_logs(tmp_path, caplog):
    bad = tmp_path / "glossary.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = _load_glossary_terms(str(bad))
    assert result == {}
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_a_json_array_is_rejected_not_a_glossary(tmp_path, caplog):
    not_an_object = tmp_path / "glossary.json"
    not_an_object.write_text('["active customer", "sold track"]', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = _load_glossary_terms(str(not_an_object))
    assert result == {}
    assert any("must be a JSON object" in r.message for r in caplog.records)


def test_a_non_string_value_is_rejected(tmp_path, caplog):
    bad_value = tmp_path / "glossary.json"
    bad_value.write_text(json.dumps({"active customer": 12}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = _load_glossary_terms(str(bad_value))
    assert result == {}
    assert any("must be a JSON object" in r.message for r in caplog.records)


def test_a_valid_file_round_trips(tmp_path):
    valid = {"widget": "a row in the widgets table with status = 'active'"}
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps(valid), encoding="utf-8")

    assert _load_glossary_terms(str(path)) == valid


# --- current_glossary_terms: the memoised, env-driven entry point ------------


def test_unset_env_means_no_glossary(monkeypatch):
    monkeypatch.delenv(GLOSSARY_FILE_ENV, raising=False)
    assert current_glossary_terms() == {}


def test_a_rotated_file_is_re_read_with_no_restart(tmp_path, monkeypatch):
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps({"a": "first"}), encoding="utf-8")
    monkeypatch.setenv(GLOSSARY_FILE_ENV, str(path))
    assert current_glossary_terms() == {"a": "first"}

    path.write_text(json.dumps({"a": "second"}), encoding="utf-8")
    # Same path (same memo key) -- this is deliberately the one case that does
    # NOT re-read, matching auth.current_identities's documented behaviour:
    # memoised on the *value of the env var*, not on the file's mtime.
    assert current_glossary_terms() == {"a": "first"}

    # A *different* path is a different memo key and does re-read.
    other = tmp_path / "glossary2.json"
    other.write_text(json.dumps({"a": "second"}), encoding="utf-8")
    monkeypatch.setenv(GLOSSARY_FILE_ENV, str(other))
    assert current_glossary_terms() == {"a": "second"}


# --- D-1: an empty resolved glossary injects nothing -------------------------


def test_d1_no_file_configured_omits_the_block_entirely(monkeypatch):
    monkeypatch.delenv(GLOSSARY_FILE_ENV, raising=False)
    reset_glossary_cache()

    with_flag_on = build_loop_system(EMPTY_SCHEMA, SCHEMA_FULL, SCHEMA_DDL, glossary=True)
    with_flag_off = build_loop_system(EMPTY_SCHEMA, SCHEMA_FULL, SCHEMA_DDL, glossary=False)

    assert "Business terms" not in with_flag_on
    assert with_flag_on == with_flag_off


def test_d1_a_configured_file_does_inject(tmp_path, monkeypatch):
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps({"widget": "a row where status = 'active'"}), encoding="utf-8")
    monkeypatch.setenv(GLOSSARY_FILE_ENV, str(path))
    reset_glossary_cache()

    system = build_loop_system(EMPTY_SCHEMA, SCHEMA_FULL, SCHEMA_DDL, glossary=True)
    assert "widget: a row where status = 'active'" in system


# --- D-2: a broken file never raises, all the way through the prompt path ----


def test_d2_a_malformed_file_does_not_raise_from_the_prompt_path(tmp_path, monkeypatch):
    path = tmp_path / "glossary.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv(GLOSSARY_FILE_ENV, str(path))
    reset_glossary_cache()

    # Must not raise -- a config typo degrades to no glossary, not a 500.
    system = build_loop_system(EMPTY_SCHEMA, SCHEMA_FULL, SCHEMA_DDL, glossary=True)
    assert "Business terms" not in system
