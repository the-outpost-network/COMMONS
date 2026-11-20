# COMMONS app image.
# Base pinned by multi-arch index digest (amd64 + arm64); update together with the tag:
#   python:3.12-slim @ 2026-11-05
ARG PYTHON_IMAGE=python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e

# --- build: install locked, hash-verified wheels into a venv -----------------
FROM ${PYTHON_IMAGE} AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
# --require-hashes: every package must match the lockfile hash.
# --no-deps: nothing outside the lockfile gets pulled in.
# --only-binary: no sdist builds (no arbitrary setup.py at build time).
RUN /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: \
        -r /tmp/requirements.lock

# --- runtime ----------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1

RUN groupadd --system --gid 10001 commons \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin commons \
 && install -d -o 10001 -g 10001 -m 0700 /data

COPY --from=build /opt/venv /opt/venv
WORKDIR /srv/commons
COPY --chown=root:root app/ ./app/
COPY --chown=root:root migrations/ ./migrations/
COPY --chown=root:root alembic.ini ./

USER 10001:10001
EXPOSE 8000

# One worker: the in-process event bus (SSE + bot) must stay consistent.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
