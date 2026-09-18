# Pinned to a patch, not the floating `3.12-slim` tag (Iteration 8 T2).
#
# The floating tag meant the shipping interpreter changed on any rebuild with
# nobody watching -- it was 3.12.14 when Iteration 8 measured it and nothing
# recorded that. `011-ship.md` §2.5 found the host two *minor* versions away;
# this line is the other half of closing that.
#
# Two stages as of Iteration 12 T2 (`specs/015-production-deployment.md` AC2).
# The builder resolves every dependency in `requirements.lock` straight into a
# target directory; the runtime stage only ever copies that directory in. No
# wheel file, cache, or compiler artifact from resolving the dependency graph
# survives into the image that serves requests.

# --------------------------------------------------------------------------
# Stage 1: install every locked dependency into a self-contained directory.
# --------------------------------------------------------------------------
FROM python:3.12.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# The lock, not the ranged file. A lockfile nothing installs from is
# decoration, and AC5 of `003` is that dependencies resolve identically on
# every run. `requirements.txt` is still copied because it is the human-edited
# source and the lock's header points at it.
COPY requirements.txt requirements.lock ./

# `--target` puts the resolved tree at a path this stage owns rather than the
# interpreter's own site-packages, so the runtime stage can copy exactly that
# directory and nothing else -- no pip cache, no wheel files, no compiler.
#
# This replaced a `RUN --mount=type=bind,from=builder` copy of built wheels
# (Iteration 12 T2), which depended on a BuildKit-only mount type. That broke
# the first time this Dockerfile was built on a platform whose builder doesn't
# implement it (Kaniko, which most multi-tenant PaaS builders use because it
# needs no Docker daemon) -- confirmed live against Back4app Containers,
# 2026-09-18: the mount silently produced an empty directory, and the runtime
# stage's `--no-index` install then failed every dependency at once. This
# version needs nothing BuildKit-specific and reaches the identical result --
# nothing installed here lands in a layer the runtime stage doesn't choose to
# copy.
RUN pip install --no-cache-dir --no-warn-script-location \
    --target=/install -r requirements.lock

# --------------------------------------------------------------------------
# Stage 2: the image that actually runs.
# --------------------------------------------------------------------------
FROM python:3.12.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/install

WORKDIR /app

# The whole resolved dependency tree, as one directory copied once. There is
# no `pip install` in this stage and nothing here ever contacts an index -- a
# dependency missing from `requirements.lock` is a stage-1 build failure, not
# a silent network install at runtime.
COPY --from=builder /install /install

# A fixed numeric UID rather than a named account (spec §7 Q-D). The number is
# what a host's user namespace, a volume's ownership and a `docker top` all
# actually see; a name is a local convenience that does not survive the trip.
RUN groupadd --system --gid 10001 querypilot \
    && useradd --system --uid 10001 --gid 10001 \
        --no-create-home --shell /usr/sbin/nologin querypilot

# The history store's volume mounts here. Creating the directory with the right
# ownership *before* the volume is declared is what makes Docker propagate that
# ownership when it initialises an empty named volume. A volume that already
# exists keeps the ownership it was created with -- that is T4's migration, and
# it is documented in the README rather than papered over with a chown at boot.
RUN mkdir -p /data && chown 10001:10001 /data

# Build context is ./api, so this lands the package at /app/api/*. That makes
# "api.main:app" resolve identically in the container and on the host.
#
# Deliberately left owned by root and readable by everyone: the process has no
# business writing its own source. `api/.dockerignore` governs what is visible
# to this line at all.
COPY . ./api/

USER 10001:10001

# Documentation only -- the process binds $PORT below, and EXPOSE has never
# published anything. Kept at the default so `docker compose` reads the same.
EXPOSE 8000

# The image carries its own liveness probe (AC4). Before Iteration 12 only
# `docker-compose.yml` had one, which meant any platform that does not read a
# compose file -- which is most of them -- ran this container with no probe at
# all. The script resolves $PORT itself, so the port is named in exactly one
# place.
#
# Invoked as a **module**, never as a file path. `python /app/api/healthcheck.py`
# puts /app/api on sys.path, where the application's own `api/http/` package
# shadows the standard library's `http` -- so `import urllib.request` fails with
# `No module named 'http.client'` and the container reports unhealthy while
# serving requests perfectly. `-m` resolves from WORKDIR /app instead, which is
# the same path the CMD's `api.main:app` already relies on.
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "api.healthcheck"]

# Shell form, because $PORT has to be expanded at start time: Render, Fly.io and
# Railway all inject it and expect the process to bind it, and before AC6 this
# container could not comply. `exec` keeps uvicorn as PID 1 so that a SIGTERM
# from the platform reaches it instead of the shell.
#
# `uvicorn` invoked as a module (`-m`), not its console-script entry point --
# a `--target` install does not reliably put that script on `PATH`, and a
# module invocation needs no `PATH` entry at all. `--no-server-header` is
# AC11: uvicorn announces itself in every response, and the deployment gains
# nothing from telling a scanner which server and which version it is talking
# to. Suppressed at the server rather than stripped in middleware, because the
# header is uvicorn's to emit and not emitting it is cheaper than removing it
# 8,000 times a day.
CMD ["sh", "-c", "exec python -m uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header"]
