# Debian slim. Pillow/pillow-heif install from wheels where available; where a
# wheel is missing for the target arch (e.g. pillow-heif on arm64), the build
# stage has the headers to compile from source, and the runtime stage carries
# the matching shared libraries.
FROM python:3.12-slim AS builder

# INSTALL_HEIC=1 (default) also installs pillow-heif for server-side HEIC
# decoding — works where a wheel exists (amd64/Render) or the system libheif is
# new enough. Set to 0 on targets that can't satisfy it (e.g. a 32-bit Pi):
#   docker build --build-arg INSTALL_HEIC=0 -t ndiro .
ARG INSTALL_HEIC=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        pkg-config \
        libheif-dev \
        libde265-dev \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-heic.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && if [ "$INSTALL_HEIC" = "1" ]; then \
         pip install --no-cache-dir --prefix=/install -r requirements-heic.txt; \
       fi

# --- Final stage: minimal runtime image --------------------------------------
FROM python:3.12-slim

# Runtime shared libraries for Pillow / pillow-heif (HEIC decode + JPEG).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libheif1 \
        libde265-0 \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Server timezone is ALWAYS UTC. User-local dates come from the client — the
# server clock is never used for them, and UTC is what the AI daily cap keys on.
ENV TZ=UTC

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

COPY --from=builder /install /usr/local

# Application code. Secrets are NOT baked in: .env is dockerignored and must be
# injected at run time (docker run --env-file / Render dashboard env vars).
COPY . .

# Run as an unprivileged user.
RUN useradd --no-create-home --user-group ndiro
USER ndiro

# Which commit this image contains. .git is dockerignored, so the hash can only
# come from the build:
#   docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
#                --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) \
#                --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) -t ndiro .
# Last on purpose: they change every commit, so no layer above them is rebuilt.
# Unset is fine — /status then says "unknown". Hosts that inject their own
# (Render's RENDER_GIT_COMMIT) need no build arg at all.
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ARG BUILD_TIME=""
ENV GIT_COMMIT=$GIT_COMMIT
ENV GIT_BRANCH=$GIT_BRANCH
ENV BUILD_TIME=$BUILD_TIME

EXPOSE $PORT

# SINGLE worker is load-bearing: the rate limiter is in-memory (memory://),
# so a second worker would hold a divergent limit state. Threads provide the
# concurrency; the 60s timeout leaves room for the AI calls' 20-25s reads.
# --no-control-socket: gunicorn >=25.1 otherwise tries to create
# $HOME/.gunicorn/gunicorn.ctl (unwritable here) and logs an error each boot;
# the gunicornc management CLI is unused with this single pinned worker.
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60 --no-control-socket --preload app:app
