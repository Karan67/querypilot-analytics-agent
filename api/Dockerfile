# Pinned to a patch, not the floating `3.12-slim` tag (Iteration 8 T2).
#
# The floating tag meant the shipping interpreter changed on any rebuild with
# nobody watching -- it was 3.12.14 when Iteration 8 measured it and nothing
# recorded that. `011-ship.md` §2.5 found the host two *minor* versions away;
# this line is the other half of closing that.
FROM python:3.12.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The lock, not the ranged file. A lockfile nothing installs from is
# decoration, and AC5 is that dependencies resolve identically on every run.
# `requirements.txt` is still copied because it is the human-edited source and
# the lock's header points at it.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Build context is ./api, so this lands the package at /app/api/*. That makes
# "api.main:app" resolve identically in the container and on the host.
COPY . ./api/

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
