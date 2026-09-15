"""File-based secrets (Iteration 12 T8, AC15 AC16 AC17).

Hermetic, with one exception that says so in its own name.

The property under test is narrow and worth stating exactly: a secret may be
delivered as a path instead of a value, the path wins when both are present,
and an unreadable path fails **closed** rather than falling back. That last one
is the interesting half. A reader that quietly used the environment variable
when its file was missing would let a deployment run on a credential nobody
remembered setting, and would look identical to one that was working.

The logging assertions are the extension of a rule this repository already
holds. `tests/test_auth_logic.py` proves the credential parser never prints a
secret; this proves the same of the reader that now sits in front of it.
"""

from __future__ import annotations

import json
import logging
import pathlib
import subprocess

import pytest

from api import config
from api.http import auth

SECRET = "s3cr3t-value-that-must-not-appear"


@pytest.fixture
def secret_file(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "groq_api_key"
    path.write_text(SECRET + "\n", encoding="utf-8")
    return path


# --- AC15: the file form, and which one wins ---------------------------------


def test_the_environment_variable_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing about the laptop path changes."""
    monkeypatch.delenv(config.file_variable("GROQ_API_KEY"), raising=False)
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    assert config.get_secret("GROQ_API_KEY") == SECRET


def test_a_file_supplies_the_secret(
    monkeypatch: pytest.MonkeyPatch, secret_file: pathlib.Path
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(secret_file))
    assert config.get_secret("GROQ_API_KEY") == SECRET


def test_the_file_wins_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch, secret_file: pathlib.Path
) -> None:
    """AC15. An operator who mounted a file meant it."""
    monkeypatch.setenv("GROQ_API_KEY", "the-stale-environment-value")
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(secret_file))
    assert config.get_secret("GROQ_API_KEY") == SECRET


def test_the_trailing_newline_is_stripped(
    monkeypatch: pytest.MonkeyPatch, secret_file: pathlib.Path
) -> None:
    """`echo secret > file` leaves one, and a credential with an invisible
    newline on the end fails in a way that looks like a wrong password."""
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(secret_file))
    assert not config.get_secret("GROQ_API_KEY").endswith("\n")


def test_interior_whitespace_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A secret is an opaque string; this is not the layer that decides which
    of its bytes are decorative."""
    path = tmp_path / "s"
    path.write_text("  spaced  secret  \n", encoding="utf-8")
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(path))
    assert config.get_secret("GROQ_API_KEY") == "  spaced  secret  "


def test_a_blank_file_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GROQ_API_KEY_FILE=` in an env file is an empty string, not absence.

    Compose defaults it to empty for every deployment that does not use the
    file form, so this is the common case rather than an edge one.
    """
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), "   ")
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    assert config.get_secret("GROQ_API_KEY") == SECRET


# --- failing closed ----------------------------------------------------------


def test_a_missing_file_does_not_fall_back_to_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The half that matters.

    A deployment whose secret file is absent is broken. Silently using a stale
    environment variable instead is how a service comes to be running on a
    credential nobody remembers setting.
    """
    monkeypatch.setenv("GROQ_API_KEY", "the-stale-environment-value")
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(tmp_path / "absent"))
    assert config.get_secret("GROQ_API_KEY") is None


def test_a_missing_file_yields_the_caller_s_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(tmp_path / "absent"))
    assert config.get_secret("GROQ_API_KEY", "fallback") == "fallback"


def test_an_unset_credential_map_still_locks_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, anonymous_client
) -> None:
    """End to end: a broken `_FILE` path must not open the perimeter.

    Iteration 10's guarantee is that unset means locked. T8 adds a new way for
    the credential map to be effectively unset, and it has to land on the same
    side.
    """
    monkeypatch.setenv(auth.USERS_ENV, json.dumps({"analyst": "letmein"}))
    monkeypatch.setenv(config.file_variable(auth.USERS_ENV), str(tmp_path / "absent"))
    assert anonymous_client.get("/").status_code == 401
    assert anonymous_client.get("/", auth=("analyst", "letmein")).status_code == 401


# --- AC17: the reader never prints what it read ------------------------------


def test_nothing_is_logged_on_the_happy_path(
    monkeypatch: pytest.MonkeyPatch, secret_file: pathlib.Path, caplog
) -> None:
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(secret_file))
    with caplog.at_level(logging.DEBUG, logger="querypilot"):
        config.get_secret("GROQ_API_KEY")
    assert SECRET not in caplog.text


def test_a_missing_file_is_reported_by_path_and_not_by_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog
) -> None:
    absent = tmp_path / "absent"
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(absent))
    with caplog.at_level(logging.DEBUG, logger="querypilot"):
        config.get_secret("GROQ_API_KEY")
    assert "absent" in caplog.text, "the operator is not told which path failed"
    assert SECRET not in caplog.text


def test_undecodable_bytes_are_never_quoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog
) -> None:
    """`UnicodeDecodeError`'s own message quotes the offending bytes, which are
    the secret. The handler deliberately does not include the exception."""
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe\x00secret-bytes")
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(path))
    with caplog.at_level(logging.DEBUG, logger="querypilot"):
        assert config.get_secret("GROQ_API_KEY") is None
    assert "secret-bytes" not in caplog.text
    assert "\\xff" not in caplog.text


def test_the_module_never_formats_the_value_it_read() -> None:
    """Read from the parsed AST, not the text.

    This module's docstring discusses logging and secrets at length, so a text
    search would match the prose explaining the rule rather than the code
    keeping it. Fifth time this repository has needed that distinction.
    """
    import ast

    source = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "read_secret_file"
    )
    for node in ast.walk(reader):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"error", "warning", "info", "debug", "exception"}:
                rendered = " ".join(ast.unparse(arg) for arg in node.args)
                assert "text" not in rendered.split(), (
                    f"the reader logs the file's contents: {rendered}"
                )


# --- what /health says, which is where it came from and never what it is -----


def test_the_health_status_names_the_source_not_the_secret(
    monkeypatch: pytest.MonkeyPatch, secret_file: pathlib.Path
) -> None:
    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(secret_file))
    status = config.secret_status("GROQ_API_KEY")
    assert status == {"source": "file", "readable": True}
    assert SECRET not in json.dumps(status)


def test_the_health_status_distinguishes_unset_from_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The one thing an operator cannot determine from outside."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv(config.file_variable("GROQ_API_KEY"), raising=False)
    assert config.secret_status("GROQ_API_KEY")["source"] == "unset"

    monkeypatch.setenv(config.file_variable("GROQ_API_KEY"), str(tmp_path / "absent"))
    broken = config.secret_status("GROQ_API_KEY")
    assert broken == {"source": "file", "readable": False}


# --- AC16: the container, which needs a daemon -------------------------------


def _docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=20,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.needs_docker
def test_the_container_configuration_carries_no_secret_value() -> None:
    """AC16, against the running container.

    This is the claim the whole task exists for and the only one that cannot be
    made without a daemon: that `docker inspect` -- and so anything holding the
    Docker socket -- cannot read the secrets back out.

    It is necessarily conditional on what the operator actually configured. A
    stack still using the environment form *will* show its values, and that is
    correct rather than a failure, so the assertion is scoped to whichever
    secrets the container reports as coming from a file.
    """
    if not _docker_available():
        pytest.skip("no docker daemon")

    inspected = subprocess.run(
        ["docker", "inspect", "querypilot-api", "--format", "{{json .Config.Env}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if inspected.returncode != 0:
        pytest.skip("querypilot-api is not running")

    env = {}
    for entry in json.loads(inspected.stdout):
        name, _, value = entry.partition("=")
        env[name] = value

    for secret in (auth.USERS_ENV, "GROQ_API_KEY"):
        path = env.get(config.file_variable(secret), "").strip()
        if not path:
            continue
        assert not env.get(secret, "").strip(), (
            f"{secret} is configured from {path} and *also* present as an "
            f"environment variable, so `docker inspect` still reads it"
        )


@pytest.mark.needs_docker
def test_the_file_form_is_wired_all_the_way_through() -> None:
    """The file variables reach the container at all.

    Without this, the test above passes trivially on a stack where the `_FILE`
    variables were never plumbed into compose -- `path` would be empty, the
    loop would `continue`, and nothing would be asserted.
    """
    if not _docker_available():
        pytest.skip("no docker daemon")

    inspected = subprocess.run(
        ["docker", "inspect", "querypilot-api", "--format", "{{json .Config.Env}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if inspected.returncode != 0:
        pytest.skip("querypilot-api is not running")

    names = {entry.partition("=")[0] for entry in json.loads(inspected.stdout)}
    assert config.file_variable(auth.USERS_ENV) in names
    assert config.file_variable("GROQ_API_KEY") in names
