# COMMONS Matrix bot image. Build context: the repository root.
# Same pinned base as the app image (python:3.12-slim, amd64 + arm64).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY bot/requirements.lock /tmp/requirements.lock
# Hash-checked wheels only; python-olm ships manylinux wheels with libolm built in.
RUN /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: \
        -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 commons \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin commons \
 && install -d -o 10001 -g 10001 -m 0700 /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv/commons
# The bot shares only the UI strings with the app, nothing else.
COPY --chown=root:root app/__init__.py ./app/__init__.py
COPY --chown=root:root app/i18n/ ./app/i18n/
COPY --chown=root:root bot/ ./bot/
USER 10001:10001
CMD ["python", "-m", "bot.main"]
