"""Measure the deployment surface. An instrument, not a report.

    python -m tools.deploy_census
    python tools/deploy_census.py --output specs/015-deployment-census.json

Iteration 12 changes how QueryPilot is packaged and run, and the case for each
change rests on a number: the image is this many megabytes, the container runs
as this user, this many routes answer anonymously. **A measurement that survives
only in a conversation is not a measurement.** Numbers transcribed from a
terminal into a spec cannot be re-derived six weeks later, cannot be diffed
against the same numbers after the change, and cannot be shown to be stale. So
the instrument is committed and the numbers are its output: `--output` before
the work and `--output` again after it produces a before/after pair that anyone
can regenerate with one command.

Everything here is collected at run time from `docker` and from HTTP against the
running stack. Nothing is hardcoded -- not the image size, not the Python
version, not the layer count. Where a probe fails, the field records `null` and
`errors` names what failed, because a census that guesses is worse than one with
a hole in it. `errors` and `null` stay in exact step, so a probe that succeeded
only on its second choice goes in `notes` instead. The script exits non-zero
only if *nothing* could be collected, which means the stack is down rather than
a single probe being unsupported.

**Keys, never values, from `/health`.** The body is walked for its key paths
alone. A census file is committed to the repository, and a response body is not
a thing anyone has promised is free of secrets.

Read-only by construction: `ps`, `inspect`, `history`, `images`, `top`, and one
`docker run --rm` probe that counts `__pycache__` directories in the built
image. It never builds, never restarts, and never stops a container.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULT_OUTPUT = "specs/015-deployment-census.json"
DEFAULT_IMAGE = "querypiolt-api:latest"
DEFAULT_API_CONTAINER = "querypilot-api"
DEFAULT_DB_CONTAINER = "querypilot-db"
DEFAULT_BASE_URL = "http://localhost:8000"

INSTRUCTION_LIMIT = 120
COMMAND_TIMEOUT = 120
HTTP_TIMEOUT = 20


class Census:
    """Accumulates probe results and the names of the probes that failed."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.collected = 0

    def fail(self, field: str, reason: str) -> None:
        """A field is null. `errors` and null values stay in exact step."""
        self.errors.append(f"{field}: {reason}")

    def note(self, field: str, reason: str) -> None:
        """A value was obtained, but not by the first-choice probe."""
        self.notes.append(f"{field}: {reason}")

    def record(self) -> None:
        """Count a field that actually landed. Only these keep the exit 0."""
        self.collected += 1

    def run(self, field: str, cmd: list[str]) -> str | None:
        """Run a command, returning stripped stdout or None (recording why)."""
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except FileNotFoundError:
            self.fail(field, f"{cmd[0]} not found on PATH")
            return None
        except subprocess.TimeoutExpired:
            self.fail(field, f"timed out after {COMMAND_TIMEOUT}s: {' '.join(cmd)}")
            return None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            first = detail[0] if detail else f"exit {proc.returncode}"
            self.fail(field, f"`{' '.join(cmd)}` failed: {first}")
            return None
        return proc.stdout.strip()


def key_paths(node: Any, prefix: str = "") -> list[str]:
    """Return the dotted key paths of a JSON body. Keys only -- never values."""
    paths: list[str] = []
    if isinstance(node, dict):
        for key in node:
            here = f"{prefix}.{key}" if prefix else str(key)
            paths.append(here)
            paths.extend(key_paths(node[key], here))
    elif isinstance(node, list):
        for item in node:
            paths.extend(key_paths(item, f"{prefix}[]" if prefix else "[]"))
    return sorted(set(paths))


def env_value(env: list[str], name: str) -> str | None:
    for entry in env:
        if entry.startswith(f"{name}="):
            return entry.split("=", 1)[1]
    return None


def collect_image(census: Census, image: str) -> dict[str, Any]:
    """Image identity, size, layer breakdown and declared runtime config."""
    out: dict[str, Any] = {
        "reference": image,
        "python_version": None,
        "size_bytes": None,
        "size_reported": None,
        "rootfs_layers": None,
        "history_entries": None,
        "config_user": None,
        "config_user_declared": None,
        "config_cmd": None,
        "healthcheck_declared": None,
        "history": None,
        "pycache_dirs": None,
    }

    raw = census.run("image.inspect", ["docker", "image", "inspect", image])
    if raw is not None:
        try:
            spec = json.loads(raw)[0]
        except (ValueError, IndexError, KeyError) as exc:
            census.fail("image.inspect", f"unparseable inspect output: {exc}")
        else:
            census.record()
            config = spec.get("Config", {})
            out["size_bytes"] = spec.get("Size")
            out["rootfs_layers"] = len(spec.get("RootFS", {}).get("Layers", []))
            # An absent USER is a finding, not a failed probe, and null here
            # must keep meaning "not collected" -- so record the empty string
            # the image actually carries and flag the absence separately.
            declared_user = config.get("User") or ""
            out["config_user"] = declared_user
            out["config_user_declared"] = bool(declared_user)
            out["config_cmd"] = config.get("Cmd")
            out["healthcheck_declared"] = bool(config.get("Healthcheck"))
            out["python_version"] = env_value(config.get("Env") or [], "PYTHON_VERSION")
            if out["python_version"] is None:
                census.fail("image.python_version", "PYTHON_VERSION absent from image env")

    reported = census.run(
        "image.size_reported",
        ["docker", "images", "--format", "{{.Size}}", image],
    )
    if reported is not None:
        out["size_reported"] = reported.splitlines()[0].strip() if reported else None
        if not out["size_reported"]:
            census.fail("image.size_reported", f"no `docker images` row for {image}")
        else:
            census.record()

    history = census.run(
        "image.history",
        [
            "docker", "history", "--no-trunc",
            "--format", "{{.Size}}\t{{.CreatedBy}}", image,
        ],
    )
    if history is not None:
        layers: list[dict[str, Any]] = []
        for line in history.splitlines():
            if not line.strip():
                continue
            size, _, instruction = line.partition("\t")
            instruction = " ".join(instruction.split())
            if len(instruction) > INSTRUCTION_LIMIT:
                instruction = instruction[:INSTRUCTION_LIMIT] + "..."
            layers.append({"size": size.strip(), "instruction": instruction})
        if layers:
            census.record()
        else:
            census.fail("image.history", "no history rows returned")
        out["history"] = layers or None
        out["history_entries"] = len(layers) or None

    # A build-context probe: bytecode caches ship in the image unless excluded.
    pycache = census.run(
        "image.pycache_dirs",
        [
            "docker", "run", "--rm", "--entrypoint", "sh", image,
            "-c", "find / -name __pycache__ -type d 2>/dev/null | wc -l",
        ],
    )
    if pycache is not None:
        tail = pycache.splitlines()[-1].strip() if pycache.splitlines() else ""
        if tail.isdigit():
            out["pycache_dirs"] = int(tail)
            census.record()
        else:
            census.fail("image.pycache_dirs", f"non-numeric probe output: {tail!r}")

    return out


def collect_container(census: Census, name: str) -> dict[str, Any]:
    """The runtime process user and published ports of a running container."""
    out: dict[str, Any] = {
        "name": name,
        "running": None,
        "process_users": None,
        "process_user_method": None,
        "ports": None,
    }

    row = census.run(
        f"container.{name}.ps",
        [
            "docker", "ps", "--filter", f"name=^/{name}$",
            "--format", "{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}",
        ],
    )
    if row is not None:
        line = row.splitlines()[0] if row.splitlines() else ""
        if not line.strip():
            out["running"] = False
            census.fail(f"container.{name}", "no running container with that name")
            return out
        fields = line.split("\t")
        census.record()
        out["running"] = True
        out["image"] = fields[1] if len(fields) > 1 else None
        out["ports"] = fields[2] if len(fields) > 2 else None
        out["status"] = fields[3] if len(fields) > 3 else None

    # `docker top -o user` is the direct question, but the ps(1) inside some
    # daemons' helper images omits the PID column it needs. Fall back to the
    # unformatted table and read its UID column rather than reporting nothing.
    method = "docker top -o user"
    users = census.run(f"container.{name}.top", ["docker", "top", name, "-o", "user"])
    if users is None:
        # The value is not lost, so this is a note rather than an error: the
        # `errors` list means "this field is null" and nothing else.
        fallback = census.errors.pop()
        census.note(f"container.{name}.top", f"fell back to the UID column ({fallback})")
        users = census.run(f"container.{name}.top", ["docker", "top", name])
        method = "docker top (UID column)"
    if users is not None:
        lines = [line for line in users.splitlines() if line.strip()]
        if len(lines) < 2:
            census.fail(f"container.{name}.top", "no process rows returned")
        else:
            out["process_users"] = sorted({line.split()[0] for line in lines[1:]})
            out["process_user_method"] = method
            census.record()

    return out


def probe(census: Census, field: str, method: str, url: str) -> dict[str, Any] | None:
    """One HTTP request. A 4xx/5xx is an observation, not a probe failure."""
    data = None
    headers = {}
    if method == "POST":
        data = json.dumps({"question": "census probe"}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            census.record()
            return {
                "status": response.status,
                "headers": response.headers,
                "body": response.read(),
            }
    except urllib.error.HTTPError as exc:
        census.record()
        return {"status": exc.code, "headers": exc.headers, "body": exc.read()}
    except (urllib.error.URLError, OSError) as exc:
        census.fail(field, f"{method} {url} unreachable: {exc}")
        return None


def collect_http(census: Census, base_url: str) -> dict[str, Any]:
    """Status codes, header names, and the key shape of the health body."""
    base = base_url.rstrip("/")
    out: dict[str, Any] = {
        "base_url": base,
        "health_header_names": None,
        "health_server": None,
        "health_body_keys": None,
        "status": {},
    }

    health = probe(census, "http.health", "GET", f"{base}/health")
    if health is not None:
        out["status"]["GET /health"] = health["status"]
        out["health_header_names"] = sorted(k.lower() for k in health["headers"].keys())
        out["health_server"] = health["headers"].get("server")
        try:
            body = json.loads(health["body"].decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            census.fail("http.health_body_keys", f"body is not JSON: {exc}")
        else:
            # Keys only. The census is committed; values are not ours to publish.
            out["health_body_keys"] = key_paths(body)
            census.record()

    for label, method, path in (
        ("GET /", "GET", "/"),
        ("POST /ask anonymous", "POST", "/ask"),
        ("GET /nope", "GET", "/nope"),
    ):
        result = probe(census, f"http.{label}", method, f"{base}{path}")
        out["status"][label] = result["status"] if result is not None else None

    return out


def build(census: Census, args: argparse.Namespace) -> dict[str, Any]:
    # Deliberately not counted as a collection: the commit is metadata about
    # the census, not a measurement of the deployment.
    commit = census.run("commit", ["git", "rev-parse", "--short", "HEAD"])
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "generated_at": generated_at,
        "commit": commit,
        "generated_by": "tools/deploy_census.py",
        "argv": sys.argv[1:],
        "targets": {
            "image": args.image,
            "api_container": args.api_container,
            "db_container": args.db_container,
            "base_url": args.base_url,
        },
        "image": collect_image(census, args.image),
        "containers": {
            "api": collect_container(census, args.api_container),
            "db": collect_container(census, args.db_container),
        },
        "http": collect_http(census, args.base_url),
        "notes": census.notes,
        "errors": census.errors,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Census the deployment surface: image, containers, HTTP.",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help=f"where to write the census (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--image", default=DEFAULT_IMAGE,
                        help=f"image reference (default: {DEFAULT_IMAGE})")
    parser.add_argument("--api-container", default=DEFAULT_API_CONTAINER,
                        help=f"api container name (default: {DEFAULT_API_CONTAINER})")
    parser.add_argument("--db-container", default=DEFAULT_DB_CONTAINER,
                        help=f"db container name (default: {DEFAULT_DB_CONTAINER})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"base URL of the running api (default: {DEFAULT_BASE_URL})")
    args = parser.parse_args(argv)

    census = Census()
    report = build(census, args)

    if census.collected == 0:
        print("nothing could be collected -- is the stack up?", file=sys.stderr)
        for error in census.errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    path = pathlib.Path(args.output)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, sort_keys=False)
        handle.write("\n")

    print(f"wrote {path}")
    print(f"probes collected: {census.collected}   failed: {len(census.errors)}")
    for note in census.notes:
        print(f"  note: {note}")
    for error in census.errors:
        print(f"  null: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
