"""Iteration 15 — the Render blueprint (`019-production-deployment.md` AC1-AC5).

Hermetic: parses `render.yaml` and checks paths on disk. No database, no
network, no `needs_db` marker.

Every assertion reads the parsed YAML structure or the filesystem, never the
raw text, per the standing rule that a "must/must not appear" test has to
read parsed data rather than risk matching a comment or a docstring.
"""

from __future__ import annotations

import pathlib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RENDER_YAML_PATH = REPO_ROOT / "render.yaml"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# Every environment variable api/config.py's secret readers or
# api/targets.py's target registration need for the deployed instance to
# work at all.
REQUIRED_ENV_VAR_KEYS = {
    "QUERYPILOT_DATABASE_URL",
    "QUERYPILOT_PAGILA_DATABASE_URL",
    "GROQ_API_KEY",
    "QUERYPILOT_USERS",
}


def blueprint() -> dict:
    return yaml.safe_load(RENDER_YAML_PATH.read_text(encoding="utf-8"))


def web_service() -> dict:
    services = blueprint()["services"]
    web = [s for s in services if s.get("type") == "web"]
    assert len(web) == 1, "expected exactly one web service in render.yaml"
    return web[0]


# --- AC1/AC2: the service builds the right Dockerfile and is health-checked -


def test_ac1_the_service_is_a_docker_web_service():
    svc = web_service()
    assert svc["env"] == "docker"
    assert svc["dockerfilePath"] == "./api/Dockerfile"
    assert svc["dockerContext"] == "./api"


def test_ac2_the_health_check_path_is_health():
    assert web_service()["healthCheckPath"] == "/health"


# --- AC3: every variable the deployed instance needs is declared ------------


def test_ac3_every_required_env_var_is_declared():
    declared = {entry["key"] for entry in web_service()["envVars"]}
    missing = REQUIRED_ENV_VAR_KEYS - declared
    assert not missing, f"render.yaml is missing: {sorted(missing)}"


# --- AC4: no _FILE indirection or mounted-secret path on this leg -----------


def test_ac4_no_file_indirection_or_mounted_secret_path():
    """Only the two secret-shaped `_FILE` siblings are banned here
    (`api/config.py`'s `FILE_SUFFIX` convention for `GROQ_API_KEY` and
    `QUERYPILOT_USERS`) — not every variable that happens to end in
    `_FILE`. `QUERYPILOT_GLOSSARY_FILE` is a baked-in path to a file that
    ships with the image (`docker-compose.yml`'s own comment on it says so),
    unrelated to the secret-indirection mechanism, and is expected to appear.
    """
    banned_keys = {f"{secret}_FILE" for secret in ("GROQ_API_KEY", "QUERYPILOT_USERS")}
    for entry in web_service()["envVars"]:
        key = entry["key"]
        value = str(entry.get("value", ""))
        assert key not in banned_keys, f"{key} is a secret _FILE variable"
        assert "/run/secrets" not in value, f"{key} references /run/secrets"


# --- AC5: the Dockerfile and context this blueprint names actually exist ----


def test_ac5_the_dockerfile_and_context_exist():
    svc = web_service()
    dockerfile = (REPO_ROOT / svc["dockerfilePath"]).resolve()
    context = (REPO_ROOT / svc["dockerContext"]).resolve()
    assert dockerfile.is_file(), dockerfile
    assert context.is_dir(), context
    assert dockerfile.is_relative_to(context)


# --- Independence guard (specs/019 §7 Q-E) -----------------------------------


def test_render_yaml_does_not_depend_on_docker_compose():
    """`specs/019` §7 Q-E: additive, not coupled to the local dev stack.

    Checked against the *parsed* service fields, not the raw file text —
    the file's own comments legitimately mention `docker-compose.yml` for
    documentation (see its header), which is not the same thing as the
    blueprint depending on it. What must not happen is any field value
    naming the compose file or one of its container-network hostnames
    (`db`, `pagila-db`), which would mean this service could not actually
    build or run without that file also being present.
    """
    svc = web_service()
    values = [svc["dockerfilePath"], svc["dockerContext"], svc["healthCheckPath"]]
    values += [str(entry.get("value", "")) for entry in svc["envVars"]]
    blob = " ".join(values)
    assert "docker-compose" not in blob
    assert "compose.yml" not in blob
    assert "@db:" not in blob
    assert "@pagila-db:" not in blob
