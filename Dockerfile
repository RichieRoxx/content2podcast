# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder
RUN pip install --no-cache-dir uv==0.8.17
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first so this layer is cached until the lock file changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself, non-editable so the venv is self-contained.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

ARG UID=1000
ARG GID=1000
ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown

LABEL org.opencontainers.image.title="content2podcast" \
      org.opencontainers.image.description="Turns blog and news articles into podcast episodes" \
      org.opencontainers.image.source="https://github.com/RichieRoxx/content2podcast" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# An unprivileged user; pick UID/GID so that bind-mounted directories are writable:
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
RUN groupadd --gid "${GID}" podcast \
    && useradd --uid "${UID}" --gid "${GID}" --no-create-home --home-dir /data \
        --shell /usr/sbin/nologin podcast \
    && install -d -o podcast -g podcast /config /data /srv/podcast

COPY --from=builder /app/.venv /app/.venv

# /config   configuration (mount read-only): config.yaml, sources.yaml, optionally .env
# /data     state: database, scratch space, status file
# /srv/podcast  the published feed and episodes (serve it with a web server)
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    C2P_CONFIG=/config/config.yaml \
    C2P_PATHS__DATA_DIR=/data \
    C2P_PATHS__OUTPUT_DIR=/srv/podcast
VOLUME ["/config", "/data", "/srv/podcast"]
WORKDIR /data
USER podcast

ENTRYPOINT ["podcast"]
CMD ["daemon"]

# Healthy once the daemon has a fresh heartbeat and a recent successful run.
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
    CMD ["podcast", "health"]
