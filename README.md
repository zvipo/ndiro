# Ndiro 🥦

**Ndiro** (Shona: "plate") is a small multi-user meal-logging web app for tracking
**viscous soluble fiber** (the cholesterol-lowering kind, dietician-driven).
Users log meals — description, optional context note, optional photo, fiber grams
(typed, tap-added from a dietician's food guide, or AI-estimated) — and review a
monthly chart against the 20 g/day Portfolio Diet goal. Read-only share links let
a dietician follow along without an account.

Flask + DynamoDB + S3 + Google sign-in + optional OpenAI, in one Docker
container. Proof-of-concept scale: ~100 users.

## How accounts work

Anyone can sign in with Google. A first sign-in creates a **pending** account and
lands on a waiting page; an admin approves or rejects it at `/admin`. Emails
listed in `ADMIN_EMAILS` become admins automatically on their first sign-in.
`MAX_USERS` (default 100) caps account creation server-side.

Everything a user logs is private to them. Admins see account metadata only —
there is no admin view of anyone's meals or photos. Users can delete their
account (and every trace of their data) themselves in Settings. See `/privacy`.

## Local development

```bash
pip install -r requirements.txt
cp env_template.txt .env        # then fill it in — see below
python app.py                   # http://localhost:5000
```

`.env` needs at minimum:

- `SECRET_KEY` — required; the app refuses to boot without it. Generate:
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- AWS credentials (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION`)
  for an IAM user allowed to use the four DynamoDB tables and the S3 bucket.
  The tables (`ndiro-users`, `ndiro-meals`, `ndiro-shares`, `ndiro-invites`)
  are **created automatically on first boot** (on-demand billing, no GSIs) —
  scope the IAM policy to an `ndiro-*` table wildcard so new tables keep
  working.
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` — an
  OAuth 2.0 *Web application* client from Google Cloud Console. The redirect
  URI must exactly match (`http://localhost:5000/callback` for dev).
- `ADMIN_EMAILS` — your email, so your first sign-in bootstraps as admin.

Optional: `S3_BUCKET` (a **private** bucket; without it photo upload is disabled
and text meals still work) and `OPENAI_API_KEY` (without it the AI estimate
buttons don't render). `AI_DAILY_LIMIT` caps estimates per user per UTC day.

Never commit `.env` — it is gitignored and dockerignored on purpose.

### Tests

Stub-based tests (in-memory DynamoDB/S3 fakes, stubbed Google/OpenAI — no
credentials or network needed) drive the real Flask app end to end:

```bash
python tests/test_m1_auth.py       # sign-in, statuses, approval, MAX_USERS
python tests/probe_cross_user.py   # tenant isolation: cross-user probe
python tests/test_m3_shares.py     # share links, identical 404s, account deletion
python tests/test_m4_ai.py         # AI caps, refunds, rate limits
```

## Deploying

Build the container:

```bash
docker build -t ndiro .
# LOCAL testing only — bound to loopback, never all interfaces:
docker run -d --name ndiro --restart unless-stopped \
    --env-file /path/to/.env -p 127.0.0.1:8000:8000 ndiro
```

**Do not publish the port on a public interface.** The app runs behind a
single trusted reverse proxy and sets `ProxyFix(x_for=1)`, so it trusts the
`X-Forwarded-For` header — a client that can reach gunicorn directly could
spoof it to bypass the per-IP rate limits, and `Secure` session cookies won't
work without the proxy's TLS. In production the container must be reachable
**only** through the TLS reverse proxy (see the Caddy section: shared Docker
network, no host ports published).

Notes that matter:

- **Run exactly one gunicorn worker** (the Dockerfile CMD does). The rate
  limiter is in-memory; more workers = divergent limits.
- The container runs with `TZ=UTC` deliberately. Clients send their own local
  dates; the server never stamps user-local dates from its own clock.
- Secrets are injected at run time (`--env-file`); nothing is baked into the
  image.

### Raspberry Pi (or any small host) behind Caddy

Run the container on a Docker network shared with a `caddy:2` container and
reverse-proxy `ndiro:8000` — no host ports published. A minimal Caddyfile site
block:

```
your.domain.example {
    reverse_proxy ndiro:8000
}
```

Caddy terminates TLS; the app's `ProxyFix(x_proto, x_host)` trusts the
forwarded scheme/host so OAuth redirects and the `Secure` session cookie work.
Set `GOOGLE_REDIRECT_URI=https://your.domain.example/callback` (and add it to
the Google client). Deploys are git-based: pull, `docker build`, recreate the
container **keeping the `--env-file` flag**.

### Render.com

Create a **Web Service** from the repo in the Render dashboard, with runtime
**Docker** — there is deliberately no `render.yaml`; the Dockerfile's `CMD` is
the start command.

1. New → Web Service → connect the repo, branch `main`.
2. Instance type: the smallest works for a PoC.
3. Environment: add every variable from `env_template.txt` with real values
   (`GOOGLE_REDIRECT_URI=https://<your-service>.onrender.com/callback`).
4. Health check path: `/health`.

Add the Render URL as an authorized redirect URI on the Google client.

## Architecture

See `CLAUDE.md` for the full map (modules, DynamoDB schemas, security
checklist). Short version: `app.py` (routes) + `config.py` (env/constants) +
`db.py` (DynamoDB/S3, all access keyed by the session's user id) + `auth.py`
(Google OAuth, fresh per-request status checks) + `ai.py` (estimators);
`templates/base.html` carries the shared dark/light skin.

## License

Apache-2.0 — see `LICENSE`.
