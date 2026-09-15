# Pinned to a patch, not the floating `3.12-slim` tag (Iteration 8 T2).
#
# The floating tag meant the shipping interpreter changed on any rebuild with
# nobody watching -- it was 3.12.14 when Iteration 8 measured it and nothing
# recorded that. `011-ship.md` §2.5 found the host two *minor* versions away;
# this line is the other half of closing that.
#
# Two stages as of Iteration 12 T2 (`specs/015-production-deployment.md` AC2).
# The builder resolves and compiles wheels; the runtime installs from them with
# the index switched off and then discards them. Nothing that was needed to
# *build* a dependency survives into the image that serves requests.

# --------------------------------------------------------------------------
# Stage 1: build the wheels.
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

# `pip wheel` resolves once, here, where a compiler is allowed to exist. The
# runtime stage never contacts an index, so a dependency that is missing from
# this directory is a build failure rather than a silent network install.
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock

# --------------------------------------------------------------------------
# Stage 2: the image that actually runs.
# --------------------------------------------------------------------------
FROM python:3.12.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements.lock ./

# The wheels arrive as a **bind mount**, not a COPY, and this is the whole
# difference between a multi-stage build that works and one that looks like it
# does. The first version of this file copied /wheels in, installed from it,
# and deleted it in the same RUN. Measurement said the image grew by 39MB: the
# COPY had already committed a 19.3MB layer, and deleting the files in a later
# layer does not reclaim the bytes -- `/wheels` was absent from the filesystem
# and still present in the image. A bind mount is visible to this one command
# and is never committed to any layer.
#
# `--no-index` is the other load-bearing flag. Without it this line would
# quietly fall back to PyPI and the builder stage would be theatre.
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock

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
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "/app/api/healthcheck.py"]

# Shell form, because $PORT has to be expanded at start time: Render, Fly.io and
# Railway all inject it and expect the process to bind it, and before AC6 this
# container could not comply. `exec` keeps uvicorn as PID 1 so that a SIGTERM
# from the platform reaches it instead of the shell.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
