# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Ndiro** — a multi-user Flask web app for logging meals and tracking **one
micro-nutrient per user**: **viscous soluble fiber** by default (dietician-
driven, cholesterol-lowering), or another micro from the curated
`NUTRIENT_CATALOG` (closed set; goal editable, direction fixed) chosen in
settings. Backed by DynamoDB + S3 with Google sign-in, native email/password
accounts (email-verified, SES-mailed reset links), and optional OpenAI
estimators. PoC scale: ~100 users,
one Docker container, single gunicorn worker. **This repo is public: never
commit secrets, bucket names, hostnames, IPs, or emails.** All config is env
vars (`env_template.txt` has placeholders; `.env` is git- and docker-ignored).

## Commands

```bash
pip install -r requirements.txt
python app.py                        # dev server, port 5000 (needs .env with SECRET_KEY)
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 60 --no-control-socket --preload app:app

./build.sh                           # docker build + the GIT_COMMIT/GIT_BRANCH/BUILD_TIME
                                     # stamp /status needs (.git never enters the image)

# Stub tests — no credentials or network needed; run ALL of these after changes:
python tests/test_m1_auth.py         # auth/approval/MAX_USERS
python tests/probe_cross_user.py     # tenant isolation (THE security test)
python tests/test_m3_shares.py       # shares, identical 404s, account deletion
python tests/test_m4_ai.py           # AI caps/refunds/rate limits
python tests/test_m5_checklist.py    # security-checklist re-verification (admin payload, cookies)
python tests/test_m6_nutrient.py     # per-user tracked micro (settings/gating)
python tests/test_m7_invites.py      # invite links (auto-approve, single-use)
python tests/test_m8_photos.py       # photo proxy + cache (scoping, 304s, LRU)
python tests/test_m9_status.py       # /status build stamp (public page leaks no config)
python tests/test_m10_monitor.py     # /admin/monitor instance stats (counts only)
python tests/test_m11_autolog.py     # async auto-log spool (queue, cap, dead-letter)
python tests/test_m12_native.py      # native accounts (signup/verify/reset, no-oracle)
```

There is no linter or build step. `SECRET_KEY` is required at import — config.py
raises without it (tests set their own).

## Modules (flat by design — approachable for contributors)

- **`app.py`** — Flask app + ALL routes; the table of contents. All routes at
  root so the Google redirect URI and root-absolute template paths never move.
- **`config.py`** — env loading, SECRET_KEY hard-fail, `FIBER_GUIDE` (the
  dietician's food table — single source of truth for the tap-to-add UI **and**
  the AI prompt), `VISCOUS_FIBER_GOAL_G` (20, Portfolio Diet),
  `NUTRIENT_CATALOG` (the closed set of selectable micros — key/label/unit/
  default goal/direction; keys are hand-chosen because they double as form
  field names, nutrients map keys, and AI schema properties, so NEVER let
  user input become a key), the build stamp (`GIT_COMMIT`/`GIT_BRANCH`/
  `GIT_COMMIT_TITLE`/`BUILD_TIME`/`GITHUB_REPO_URL` → the `/status` page and `/health`: env first
  — including Render's `RENDER_GIT_*` — then a `.git` read for dev servers,
  every value regex-validated because the hash and repo URL end up in an
  `href`; unknown is a normal state), and `resolve_nutrient(user_row)` — the single
  source of truth for the user's tracked micro
  `{key,label,unit,goal,direction,is_default}`; absent attrs or
  `nutrient_key='fiber_g'` fall back to the fiber default (no migration;
  fiber honors a stored personalized goal).
- **`db.py`** — boto3 setup, auto-create of the four tables on boot,
  users/meals/shares/invites accessors, S3 photo helpers, the AI daily-use
  counter, and the `scan_*_stats` collectors behind `/admin/monitor` (full
  scans, page-capped, reporting `truncated` rather than lying; the meals one
  is projected to `user_id`+`date`; the `per_user` maps are internal
  intermediates, never serialized — see invariant #7).
  Table handles are functions (`users_table()` etc.) so tests swap in fakes.
- **`auth.py`** — Google OAuth (server-side code exchange via `requests`, no
  JWT lib; token trusted because it comes from Google over TLS), `current_user()`,
  `approved_required` / `admin_required`, `_safe_next`.
- **`native_auth.py`** — native (email/password) account primitives: werkzeug
  scrypt hashing (+ a dummy-hash verify to flatten no-such-user AND locked
  timing), `new_user_id(email)` (`nat-` + SHA-256(email)[:32] —
  DETERMINISTIC so db.create_native_user's conditional put is the atomic
  one-native-account-per-email guarantee; mirrors Google's stable sub, never
  collides with one, path-safe for the S3 prefix), emailed-token minting
  (256-bit random in the link, SHA-256 hash on the row), email/password
  validation (8–256 chars, no composition rules), lockout constants (10
  fails → 15 min; the atomic counter itself lives in db.py). Pure helpers —
  the session and DB writes stay in app.py/db.py.
- **`mailer.py`** — Amazon SES via boto3 `sesv2` (same credential chain as
  db.py; lazy client, 5s/10s timeouts). `EMAIL_ENABLED` requires `MAIL_FROM`
  AND (`APP_BASE_URL` or the `COOKIE_SECURE=0` dev mode) ⇒ production
  emailed links are NEVER built from the forgeable request Host (reset-link
  poisoning); disabled ⇒ signup/forgot/resend 503 while password SIGN-IN
  keeps working. Failures log `MAIL_ERROR <type> <ses-code>` only — never
  the address, body, or token. Tests monkeypatch `mailer.send`.
- **`ai.py`** — estimator prompts/schema, `_openai_estimate` (plain requests,
  strict json_schema, timeouts (5,20) text / (5,25) vision under gunicorn's 60s).
  Every entry point takes the resolved nutrient config: the fiber default keeps
  the curated guide prompt + historical `viscous_fiber_g` schema; a custom
  micro gets a generic label/unit prompt with the schema keyed by its
  `nutrient_key`. Client responses are normalized to `amount` on both paths.
  `log_failure(stage, fields)` is the ONE place a failure is recorded: it
  prints `AI_ERROR {json}` on stdout (plus an append to `AI_ERROR_LOG` when
  set) and returns a short `ref` that rides back in the error tuple
  `(message, status, refundable, ref)` — app.py shows it to the user, so a
  report maps to one log line. `stage` ∈ `request|http|parse` here, plus
  `image`/`cap` from app.py. app.py passes `log_context={'user','route'}`.
- **`autolog.py`** — the async "auto-add from photos" pipeline: `/api/auto-log`
  spools the (already normalized) JPEG plus a tiny sidecar
  (user_id/date/time/attempts — never meal content) on LOCAL disk
  (`AUTOLOG_DIR`, ephemeral by default), and ONE lazily-started daemon thread
  (`ensure_worker` — lazy because gunicorn runs `--preload`; kicked from the
  auto-log routes and `/log` so restarts recover leftovers) estimates and
  commits each entry: fresh user re-read (rejected/deleted accounts are
  discarded, never logged), AI cap consumed per photo with the same
  consume-before/refund-on-upstream-failure semantics, estimate cached on the
  sidecar across commit retries (never re-billed), cap-hit or exhausted
  retries degrade to a placeholder-description meal (the photo+time are never
  lost), and poison entries dead-letter as `*.json.dead` after `MAX_ATTEMPTS`.
  Re-running a batch is idempotent: the upload route skips a photo whose
  date+minute already has a PHOTO meal or a queued entry (the EXIF minute is
  the dedup fingerprint; text-only meals and dead letters don't block). The
  meal scan covers [`since`+`since_time` = the batch's oldest photo's EXIF
  stamp (31-day cap; the window opens AT that photo, so earlier meals on the
  since day never block), UTC tomorrow] — not just the photo's day — so a
  duplicate whose date drifted (clamped camera clock, EXIF-less file) is
  still caught; no/invalid `since` = own-day only, and the spool check stays
  exact date+time.
  Account deletion purges the user's spool via `drop_user` (strict, before the
  S3 wipe). Tests disable the thread and drive `process_once()` directly.
- **`templates/`** — `admin.html` (accounts + approve/reject) and
  `monitor.html` (the instance dashboard: aggregate tiles from
  `/api/admin/stats`, deliberately with no per-account table) are the two admin
  surfaces; `base.html` holds the
  shared skin (its menu also carries
  the running short commit next to the Status entry, injected by app.py's
  `inject_build` context processor): 14-token gruvbox
  dark/light `:root` blocks, pre-paint `localStorage('theme')` script, ☀️/🌙
  toggle (dispatches `ndiro-theme-change`; the review chart re-renders from CSS
  tokens on it). `_review_core.html`/`_review_styles.html` are shared by
  `review.html` and `share_view.html` — the share view differs only in data URL
  and chrome, and has no edit/AI affordances by construction.

## Data model (DynamoDB, on-demand, auto-created, deliberately NO GSIs — four tables)

- **users** — PK `user_id` = Google `sub` (stable; emails change), or
  `nat-{hex32}` for native accounts. `email`, `name`, `status` ∈
  `pending|approved|rejected|admin`, `created_at`,
  `approved_at?`, `ai_uses_date` (UTC day), `ai_uses_today`, native-account
  attrs (`auth_provider='native'`, `password_hash`, `email_verified`,
  `verify_token_hash`/`verify_expires_at`, `reset_token_hash`/
  `reset_expires_at`, `failed_logins`/`locked_until`, transient
  `pending_invite_token` — all on this ONE row so deletion/scans need no
  extra step; email lookups are scans, no GSI; native-email uniqueness is
  enforced by the email-derived user_id + conditional put — race-safe with
  no uniqueness constraint on the email attr itself, so a Google account
  may legitimately coexist with a native one on the same email, and
  /forgot prefers the NATIVE row), and optional
  `nutrient_key/nutrient_label/nutrient_unit/nutrient_goal/nutrient_direction`
  (the tracked micro; absent on legacy rows = fiber default, resolved at read
  time by `config.resolve_nutrient` — never migrate). The meal form field name
  and the `nutrients` map key ARE `nutrient_key`, always one of the
  `NUTRIENT_CATALOG` keys (rows written by the retired free-form settings UI
  may carry other keys; the resolver still honors them). Switching micros does
  NOT convert old meals — old days read 0 for the new key (disclosed in
  settings; values are kept under their old key, and meal edits merge
  non-active keys from the existing item so they survive).
  User counting/listing use scans — fine at ≤100 users, don't "fix" it.
- **meals** — PK `user_id`, SK `sk` = `{YYYY-MM-DD}#{meal_id}`;
  `meal_id` = `HHMMSS-{hex6}` where HHMMSS is the **client-provided** time (it
  is what sorts meals chronologically; that's why edit mode disables date/time
  inputs — they're the key). `date`/`meal_id` duplicated as plain attrs.
  `nutrients` is a Map of Decimals (`fiber_g` = viscous soluble fiber; add
  `protein_g` later by extending `_nutrients_from_form` only). A month view is
  ONE Query (`begins_with('YYYY-MM-')`); N-day windows use
  `between(f'{start}#', f'{end}#~')`. There is NO meal-data caching layer (the only cache is the photo byte LRU) — per-user
  Queries are cheap; do not port the old tracker's cache/thread machinery.
- **shares** — PK `share_token` = `token_urlsafe(24)` (192 bits). `user_id`,
  `created_at`, `expires_at?` (epoch; absent = never), `revoked`, `label?`.
  Rows are kept after revoke/expiry; listing is a filtered scan (deliberate).
- **invites** — PK `invite_token` = `token_urlsafe(24)`. Single-use expiring
  auto-approve links: `user_id` (inviter), `created_at`, `expires_at` (epoch,
  ALWAYS set — a row without it is inactive, fail-closed, unlike shares),
  `revoked`, `used_by?`/`used_at?` (set by the atomic `claim_invite`), `label?`.
  Redeemed accounts carry `invited_by` on the users row. Rows kept; filtered
  scan; capped at `MAX_ACTIVE_INVITES` active per user.

S3: photos at `users/{user_id}/meals/{date}/{meal_id}.jpg` in a private bucket;
keys built **server-side only** in db.py; served THROUGH the app (never
presigned): `/photo/<date>/<meal_id>` (owner session) and
`/s/<token>/photo/...` (token-scoped), both 120/min, backed by a
byte-budgeted in-process LRU (`PHOTO_CACHE_MB`, default 64 — valid because
of the single gunicorn worker) with `?v=sha1(updated_at)` versioned URLs +
ETag/304 so browsers cache too (`Cache-Control: private`; owner max-age 1y
immutable, share max-age 1 day so revoked recipients' caches age out).
`get_photo_bytes` refuses keys outside the owner's prefix; replacement
reuses the same key (no orphans; the version bump busts caches);
delete_photo/delete_user_photos purge the LRU.

## Security invariants (breaking any of these is a P0 — re-verify after changes)

1. Every meal read/write keys on `user_id = session['user_id']` — user_id
   NEVER comes from URL/query/form. `tests/probe_cross_user.py` enforces this.
2. `/s/<token>` + `/s/<token>/meals` + `/s/<token>/photo/...`: read-only,
   scoped to the token row's user_id; the session is read ONLY for menu
   chrome on the page, never for data access (the data and photo routes stay
   fully session-independent); the nutrient config shown comes from the token
   row's OWNER, never the viewer; share photo dead states (dead token,
   missing meal, missing photo) share ONE byte-identical 404 (`_share_404`);
   missing/revoked/expired tokens are byte-identical 404s (no enumeration
   oracle — the share 404 pins `login_next='/'` so the token path never
   lands in the page).
3. `approved_required`/`admin_required` do a FRESH users-table read every
   request (a rejected user's live session must die immediately; never cache
   status in the cookie). `ADMIN_EMAILS` only bootstraps status at first
   **Google** sign-in — NEVER for native accounts (typing the admin's email
   into the signup form, even verified, must not mint an admin).
4. Session stores only `user_id` (+ transient `oauth_state`, `login_next`,
   `invite_token` — the last popped unconditionally in /callback, never
   surviving into the post-login session — and `form_token`, the native-form
   CSRF analog of oauth_state); sessions are established in exactly three
   places — `/callback`, `POST /login/password`, `POST /verify-email/<token>`
   — always clear-then-set, and never for an unverified native account;
   cookie is Secure/HttpOnly/SameSite=Lax, 30 days; ProxyFix(x_proto, x_host).
5. OAuth `state` CSRF via `session.pop` comparison; `_safe_next` allows only
   relative paths (no `//`, no `\`).
6. Rate limits (Flask-Limiter, `memory://` — valid ONLY with one gunicorn
   worker, which the Dockerfile pins): login page + `/login/google` +
   `/login/password` + callback 10/min, `/signup` 5/min, `/forgot` POST +
   `/resend-verification` 3/min (mail-sending: tightest), `/forgot` GET +
   verify/reset routes + `/api/settings/password` 10/min, `/s/*` and
   `/i/*` 30/min, photo proxy routes 600/min, invite creation 10/min,
   `/api/admin/stats` 12/min (a full scan of every table per call),
   AI 6/min/IP, global 300/min. AI also capped per user per UTC day via the
   race-safe two-call conditional counter in db.py (increment BEFORE the
   OpenAI call; refund on upstream failure only). Per-ACCOUNT password
   guessing is bounded by the lockout in invariant #12, not by these.
7. Admin surfaces show account metadata ONLY — no route or template lets an
   admin see another user's meals or photos. `/admin` shows the user rows
   (email/name/status); `/admin/monitor` + `/api/admin/stats` add INSTANCE-WIDE
   AGGREGATES and nothing else — no per-account row, id, count, or date, so the
   dashboard adds nothing to what `/privacy` already promises. Two mechanisms
   hold it there, and both are asserted by `tests/test_m10_monitor.py`:
   `db.scan_meal_stats` scans with `ProjectionExpression='user_id, #d'` so
   descriptions/contexts/nutrient values never enter the process, and
   `db.scan_photo_stats` counts S3 keys/sizes without a single `get_object`.
   The `per_user` maps those two return are INTERMEDIATES — they exist so the
   route can compute cardinalities (`logging_accounts`, `active_7d`) and detect
   orphans; nothing keyed by user_id may be serialized. Never widen the
   projection, and never add a per-account field to the payload.
8. Server logs carry user IDs and error types only — never meal descriptions,
   contexts, or photo bytes. The AI_ERROR records add call metadata (stage,
   model, sizes, timings) and the PROVIDER's own error fields (status, request
   id, error type/code/param/message, finish_reason, refusal) — things that
   describe the API call, not the meal. Lengths/sizes (`desc_len`,
   `photo_kb`, `content_len`) and key NAMES (`got_keys`) stand in for content;
   never log the description, the model's `content`, or the photo.
9. `MAX_USERS` enforced server-side at account creation — including invited
   signups (the gate runs BEFORE invite logic; a full instance never consumes
   an invite). Native signup keeps the same order (stale purge → gate →
   duplicate check → invite), and its invites are only VALIDATED at signup —
   the atomic claim happens at email verification (claim → approve → THEN
   consume the verify token, so no crash window can burn the invite with the
   account still pending), so an abandoned signup never burns a single-use
   invite. Any later signup purges every native row that is unverified +
   PENDING + expired-verify-token, BEFORE the capacity gate (such a row
   never had a session, so the row delete is the whole wipe, and it must
   not squat a MAX_USERS slot); a REJECTED unverified row is never purged —
   rejection is a ban and keeps its slot.
9b. Invite redemption is server-side ONLY (nothing from a URL sets status):
   `/i/<token>`'s four dead states (missing/revoked/expired/used) are
   byte-identical 404s; the inviter is freshly re-read at both view and
   redemption time (rejected/deleted inviters mint nothing); the claim is a
   race-safe conditional write; invite tokens are never logged; the /i/ page
   shows the inviter's name, never their email.
10. Server clock (TZ=UTC) is never used for user-local dates: meal `date` is
    required from the client (400 without it); UTC is a fallback for the time
    component only. Reads take `?anchor=` (client's local today).
11. `/status` is PUBLIC (like `/privacy` and `/health`): it shows the build
    stamp, uptime, and BOOLEANS for the optional integrations (AI, photos,
    email) — never a configuration value (no bucket, model, host, or email).
    A new field there needs a matching entry in `tests/test_m9_status.py`'s
    leak check.
12. Native credentials: passwords exist ONLY as werkzeug scrypt hashes;
    emailed verify/reset tokens are 256-bit random, stored ONLY as SHA-256
    hashes, single-use via conditional writes, and expiring (verify 24h,
    reset 1h); raw tokens/links and email addresses never appear in logs
    (`MAIL_ERROR` carries the exception type + SES code only). Responses are
    uniform — one exact sign-in error for unknown/wrong/locked (locked skips
    even the hash check; a dummy-hash verify flattens no-such-user timing),
    one check-email page for every /signup outcome, one sent page for every
    /forgot input, one byte-identical dead page for every dead emailed link —
    the only sanctioned differentials are `full.html` at capacity and the
    verify-your-email page on a CORRECT password (which proves ownership).
    Timing is part of the no-oracle contract: signup hashes BEFORE the
    duplicate branch, sign-in dummy-hashes the unknown AND locked paths, and
    account-dependent side work (SES sends, the failure counter) runs off
    the response path via `_defer` (tests set `app.ASYNC_AUTH_WORK=False`
    to run it inline). A completed password change or reset atomically
    invalidates any outstanding reset token.
    Emailed GET links never mutate (scanners prefetch) — consumption is
    POST-only. Lockout: 10 consecutive failures → 15 min, cleared by success
    or a completed reset; the counter is an atomic DynamoDB ADD (concurrent
    guesses can't lose an update). Every native users-table update is
    conditional on the row still existing — a write racing account deletion
    must never resurrect a row (DynamoDB updates upsert by default). The
    admin payload's `unverified` boolean is derived
    — hashes/tokens/providers never enter `_user_to_json`'s allowlist.

## Gotchas

- Decimal discipline: `Decimal(str(x))` into DynamoDB, `float()` out
  (`jsonify` raises on Decimal; `put_item` raises on float).
- `GOAL_G` in the review JS comes from config via the template — keep
  `VISCOUS_FIBER_GOAL_G` the only definition.
- The tests are plain scripts (no pytest); `tests/testkit.py` must be imported
  first — it sets env vars and installs the fakes before app import. Fakes
  implement the exact boto3 surface db.py uses (update expressions may mix
  SET/REMOVE/ADD clauses); if you add a new condition
  expression shape, extend `tests/fakes.py`. `mailer.send` is replaced by
  `tk.MAILER` — pull emailed links back out with `tk.extract_link`.
- Changing a native account's password does NOT invalidate its other live
  sessions (the cookie holds only `user_id`; there is no session-versioning
  machinery) — accepted at PoC scale, don't bolt one on casually.
- Account deletion order: photos → meals → shares → invites → user row LAST
  (retryable). `/admin/monitor`'s "orphans" tile counts meal rows and photo
  objects whose `user_id` has no users row — a non-zero value means that
  sequence stopped part-way.
- `/api/admin/stats` windows: meal windows are closed at BOTH ends
  (`[anchor-N+1, anchor]`) against the admin's `?anchor=` local day, because
  meal dates are client-local (invariant #10); signup windows use UTC, because
  `created_at` is a server stamp. Do not "simplify" either to the server clock.
  The route is 12/min — each call is a full scan of every table, which is cheap
  at PoC scale but not free — and deliberately has NO cache (same reason meals
  don't: per-user Queries and small scans are cheap; a cache is machinery to
  get wrong).
