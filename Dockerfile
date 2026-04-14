# syntax=docker/dockerfile:1.6
#
# Multi-stage build keeps the runtime image small:
#   1. builder  — install build tools, compile wheels into /wheels
#   2. runtime  — copy wheels, install, drop tools, switch to non-root user
#
# The bot writes a SQLite file (zakupator.db) — mount a volume at /data to
# persist it across container restarts. Path is configurable via
# DATABASE_URL env var (see docker-compose.yml).

ARG PYTHON_VERSION=3.12-slim-bookworm


# ---- builder ------------------------------------------------------------

FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# build-essential is needed by selectolax / others that compile C extensions
# on unusual architectures; the wheel cache makes the final image independent
# of these build tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md* ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .


# ---- runtime ------------------------------------------------------------

FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/zakupator.db \
    LOG_LEVEL=INFO

# Non-root user so the bot can't accidentally rewrite its own code.
RUN useradd --system --uid 1000 --home /app --shell /usr/sbin/nologin zakupator \
    && mkdir -p /data \
    && chown -R zakupator:zakupator /data

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

USER zakupator
VOLUME ["/data"]

# Long-polling loop. Quick SIGTERM exits because aiogram handles it.
CMD ["python", "-m", "zakupator"]
