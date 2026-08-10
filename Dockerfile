# Multi-stage build: compile wheels in a throwaway layer, ship a slim runtime.
FROM python:3.12-alpine AS builder

RUN apk add --no-cache --virtual .build-deps \
        gcc \
        musl-dev

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip cache purge

# --- Final stage: minimal runtime image --------------------------------------
FROM python:3.12-alpine

# Server timezone is ALWAYS UTC. User-local dates come from the client — the
# server clock is never used for them, and UTC is what the AI daily cap keys on.
ENV TZ=UTC

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Application code. Secrets are NOT baked in: .env is dockerignored and must be
# injected at run time (docker run --env-file / Render dashboard env vars).
COPY . .

# Run as an unprivileged user.
RUN adduser -D -H ndiro
USER ndiro

EXPOSE $PORT

# SINGLE worker is load-bearing: the rate limiter is in-memory (memory://),
# so a second worker would hold a divergent limit state. Threads provide the
# concurrency; the 60s timeout leaves room for the AI calls' 20-25s reads.
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60 --preload app:app
