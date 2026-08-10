# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Ndiro** — a multi-user Flask web app for logging meals and tracking **viscous
soluble fiber** (dietician-driven, cholesterol-lowering), backed by DynamoDB +
S3 with Google sign-in and optional OpenAI estimators. PoC scale: ~100 users,
one Docker container, single gunicorn worker. **This repo is public: never
commit secrets, bucket names, hostnames, IPs, or emails.** All config is env
vars (`env_template.txt` has placeholders; `.env` is git- and docker-ignored).

## Commands

```bash
pip install -r requirements.txt
python app.py                        # dev server, port 5000 (needs .env with SECRET_KEY)
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 60 --preload app:app

# Stub tests — no credentials or network needed; run all four after changes:
python tests/test_m1_auth.py         # auth/approval/MAX_USERS
python tests/probe_cross_user.py     # tenant isolation (THE security test)
python tests/test_m3_shares.py       # shares, identical 404s, account deletion
python tests/test_m4_ai.py           # AI caps/refunds/rate limits
```

There is no linter or build step. `SECRET_KEY` is required at import — config.py
raises without it (tests set their own).

## Modules (flat by design — approachable for contributors)

- **`app.py`** — Flask app + ALL routes; the table of contents. All routes at
  root so the Google redirect URI and root-absolute template paths never move.
- **`config.py`** — env loading, SECRET_KEY hard-fail, `FIBER_GUIDE` (the
  dietician's food table — single source of truth for the tap-to-add UI **and**
  the AI prompt), `VISCOUS_FIBER_GOAL_G` (20, Portfolio Diet).
- **`db.py`** — boto3 setup, auto-create of the three tables on boot,
  users/meals/shares accessors, S3 photo helpers, the AI daily-use counter.
  Table handles are functions (`users_table()` etc.) so tests swap in fakes.
- **`auth.py`** — Google OAuth (server-side code exchange via `requests`, no
  JWT lib; token trusted because it comes from Google over TLS), `current_user()`,
  `approved_required` / `admin_required`, `_safe_next`.
- **`ai.py`** — estimator prompts/schema, `_openai_estimate` (plain requests,
  strict json_schema, timeouts (5,20) text / (5,25) vision under gunicorn's 60s).
- **`templates/`** — `base.html` holds the shared skin: 14-token gruvbox
  dark/light `:root` blocks, pre-paint `localStorage('theme')` script, ☀️/🌙
  toggle (dispatches `ndiro-theme-change`; the review chart re-renders from CSS
  tokens on it). `_review_core.html`/`_review_styles.html` are shared by
  `review.html` and `share_view.html` — the share view differs only in data URL
  and chrome, and has no edit/AI affordances by construction.

## Data model (DynamoDB, on-demand, auto-created, deliberately NO GSIs)

- **users** — PK `user_id` = Google `sub` (stable; emails change). `email`,
  `name`, `status` ∈ `pending|approved|rejected|admin`, `created_at`,
  `approved_at?`, `ai_uses_date` (UTC day), `ai_uses_today`.
  User counting/listing use scans — fine at ≤100 users, don't "fix" it.
- **meals** — PK `user_id`, SK `sk` = `{YYYY-MM-DD}#{meal_id}`;
  `meal_id` = `HHMMSS-{hex6}` where HHMMSS is the **client-provided** time (it
  is what sorts meals chronologically; that's why edit mode disables date/time
  inputs — they're the key). `date`/`meal_id` duplicated as plain attrs.
  `nutrients` is a Map of Decimals (`fiber_g` = viscous soluble fiber; add
  `protein_g` later by extending `_nutrients_from_form` only). A month view is
  ONE Query (`begins_with('YYYY-MM-')`); N-day windows use
  `between(f'{start}#', f'{end}#~')`. There is NO caching layer — per-user
  Queries are cheap; do not port the old tracker's cache/thread machinery.
- **shares** — PK `share_token` = `token_urlsafe(24)` (192 bits). `user_id`,
  `created_at`, `expires_at?` (epoch; absent = never), `revoked`, `label?`.
  Rows are kept after revoke/expiry; listing is a filtered scan (deliberate).

S3: photos at `users/{user_id}/meals/{date}/{meal_id}.jpg` in a private bucket;
keys built **server-side only** in db.py; presigned GETs (1h) re-signed every
response; `presign_photo` refuses keys outside the owner's prefix; replacement
reuses the same key (no orphans).

## Security invariants (breaking any of these is a P0 — re-verify after changes)

1. Every meal read/write keys on `user_id = session['user_id']` — user_id
   NEVER comes from URL/query/form. `tests/probe_cross_user.py` enforces this.
2. `/s/<token>` + `/s/<token>/meals`: read-only, scoped to the token row's
   user_id; the session is read ONLY for menu chrome on the page, never for
   data access (`/s/<token>/meals` stays fully session-independent);
   missing/revoked/expired tokens are byte-identical 404s (no enumeration
   oracle — the share 404 pins `login_next='/'` so the token path never
   lands in the page).
3. `approved_required`/`admin_required` do a FRESH users-table read every
   request (a rejected user's live session must die immediately; never cache
   status in the cookie). `ADMIN_EMAILS` only bootstraps status at first
   sign-in.
4. Session stores only `user_id` (+ transient `oauth_state`, `login_next`);
   cookie is Secure/HttpOnly/SameSite=Lax, 30 days; ProxyFix(x_proto, x_host).
5. OAuth `state` CSRF via `session.pop` comparison; `_safe_next` allows only
   relative paths (no `//`, no `\`).
6. Rate limits (Flask-Limiter, `memory://` — valid ONLY with one gunicorn
   worker, which the Dockerfile pins): login/callback 10/min, `/s/*` 30/min,
   AI 6/min/IP, global 300/min. AI also capped per user per UTC day via the
   race-safe two-call conditional counter in db.py (increment BEFORE the
   OpenAI call; refund on upstream failure only).
7. Admin surfaces show account metadata ONLY — no route or template lets an
   admin see another user's meals or photos.
8. Server logs carry user IDs and error types only — never meal descriptions,
   contexts, or photo bytes.
9. `MAX_USERS` enforced server-side at account creation.
10. Server clock (TZ=UTC) is never used for user-local dates: meal `date` is
    required from the client (400 without it); UTC is a fallback for the time
    component only. Reads take `?anchor=` (client's local today).

## Gotchas

- Decimal discipline: `Decimal(str(x))` into DynamoDB, `float()` out
  (`jsonify` raises on Decimal; `put_item` raises on float).
- `GOAL_G` in the review JS comes from config via the template — keep
  `VISCOUS_FIBER_GOAL_G` the only definition.
- The tests are plain scripts (no pytest); `tests/testkit.py` must be imported
  first — it sets env vars and installs the fakes before app import. Fakes
  implement the exact boto3 surface db.py uses; if you add a new condition
  expression shape, extend `tests/fakes.py`.
- Photo URLs expire after 1h — a page left open longer shows broken thumbnails
  until refresh; known PoC tradeoff.
- Account deletion order: photos → meals → shares → user row LAST (retryable).
