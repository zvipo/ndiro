"""Ndiro — a multi-user meal log tracking one micro-nutrient per user
(viscous soluble fiber by default; a custom micro is configurable in settings).

Flask app + all routes (the table of contents). Support modules:
config.py (env + constants + nutrient resolver), db.py (DynamoDB/S3),
auth.py (OAuth + guards), ai.py (nutrient estimators). All routes live at
root so the Google redirect URI and root-absolute template paths never change.
"""
import calendar
import hashlib
import re
import secrets
import threading
import time
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from flask import Flask, g, jsonify, redirect, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import ai
import auth
import autolog
import config
import imaging
import db
import mailer
import native_auth

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_MONTH_RE = re.compile(r'^\d{4}-\d{2}$')
_INVITE_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')  # token_urlsafe shape
_NUTRIENT_MAX = Decimal('100000')  # grams; anything larger is nonsense / a DynamoDB overflow
_DATE_SLACK = timedelta(days=400)  # day arithmetic a validated date must survive (see _valid_date)

app = Flask(__name__)

# Behind EXACTLY ONE trusted reverse proxy (Caddy on the Pi / Render's LB).
# x_for=1 makes request.remote_addr the real client so the rate limiter
# isolates clients instead of collapsing everyone into the proxy's IP.
# Do NOT keep x_for=1 if the container is ever exposed without a proxy —
# clients could then spoof X-Forwarded-For to dodge rate limits.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = config.SECRET_KEY  # config hard-fails at import when unset
app.config.update(
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,  # =True outside local dev (see config.py)
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',  # the load-bearing CSRF control: blocks cross-site cookie attachment on writes
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=config.MAX_CONTENT_LENGTH,
)

# In-memory rate limiting is valid ONLY because gunicorn runs a single worker
# (see Dockerfile CMD). Lax global default; tight limits on auth/share/AI.
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri='memory://',
    default_limits=['300 per minute'],
)

# Auto-create missing tables at boot (logs, never crashes).
db.ensure_tables()


@app.context_processor
def inject_build():
    """Running commit for base.html's menu — the version is visible from any
    page, and /status has the detail."""
    return {'build_commit_short': config.GIT_COMMIT_SHORT}


def _utc_today_str():
    """UTC date for server-side bookkeeping (AI caps). NEVER used as a
    user-local meal date — clients send their own dates."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({'error': 'Too many requests — please slow down.'}), 429


@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'Upload too large (max 16 MB)'}), 413


# --- Pages -------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('landing.html', user=auth.current_user())


@app.route('/waiting')
def waiting():
    user = auth.current_user()
    if user is None:
        session.clear()
        return redirect('/')
    if user.get('status') in auth.APPROVED_STATUSES:
        return redirect('/log')
    if user.get('status') != 'pending':
        session.clear()
        return redirect('/')
    return render_template('waiting.html', user=user)


@app.route('/privacy')
def privacy():
    return render_template('privacy.html', user=auth.current_user())


@app.route('/admin')
@auth.admin_required
def admin_page():
    return render_template('admin.html', user=g.user)


@app.route('/health')
def health():
    """Liveness probe. Carries the running commit so a deploy can be verified
    with one curl; /status is the same facts for humans."""
    return jsonify({'status': 'healthy',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'commit': config.GIT_COMMIT_SHORT,
                    'branch': config.GIT_BRANCH}), 200


def _uptime_str(seconds):
    """Coarse, human uptime: 3d 4h / 4h 12m / 12m / 40s."""
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m'
    return f'{seconds}s'


@app.route('/status')
@limiter.limit('30 per minute')
def status_page():
    """What is running right now: the deployed commit (linked to the public
    repo) plus which optional integrations are configured. Public on purpose —
    the source is public, and it must be checkable without signing in. It shows
    NO configuration values: only whether a feature is on, never the bucket,
    the model key, or any host."""
    return render_template(
        'status.html',
        user=auth.current_user(),
        commit=config.GIT_COMMIT,
        commit_short=config.GIT_COMMIT_SHORT,
        commit_title=config.GIT_COMMIT_TITLE,
        commit_url=config.commit_url(),
        branch=config.GIT_BRANCH,
        build_time=config.BUILD_TIME,
        repo_url=config.GITHUB_REPO_URL,
        uptime=_uptime_str(time.time() - config.STARTED_AT),
        started_at=datetime.fromtimestamp(config.STARTED_AT, timezone.utc)
                           .strftime('%Y-%m-%d %H:%M UTC'),
        server_time=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        ai_enabled=bool(config.OPENAI_API_KEY),
        photos_enabled=bool(config.S3_BUCKET),
        email_enabled=bool(config.EMAIL_ENABLED))


# --- Sign-in (chooser page + Google OAuth + native email/password) -----------

def _form_token():
    """Session-bound CSRF token for the UNauthenticated auth forms — the
    native-form analog of oauth_state. Minted on the GET pages, echoed as a
    hidden field, compared (without popping — multiple tabs) on POST.
    SameSite=Lax keeps the cookie off cross-site POSTs, so a forged POST
    can't present a matching value."""
    token = session.get('form_token')
    if not token:
        token = secrets.token_urlsafe(16)
        session['form_token'] = token
    return token


def _check_form_token():
    """True when the POSTed form_token matches the session's."""
    expected = session.get('form_token')
    supplied = request.form.get('form_token')
    return bool(expected) and supplied == expected


def _login_page_args():
    """Sanitized next/invite carried through the login/signup forms."""
    next_target = auth._safe_next(request.values.get('next'), default='/log')
    invite = request.values.get('invite')
    if not (invite and _INVITE_RE.match(invite)):
        invite = None
    return next_target, invite


def _render_login(next_target, invite, error=None, email='', notice=None,
                  invite_valid=False, status=200):
    return render_template(
        'login.html',
        user=None,
        google_enabled=bool(config.GOOGLE_CLIENT_ID),
        email_enabled=mailer.enabled(),
        next_target=next_target,
        invite=invite,
        invite_valid=invite_valid,
        form_token=_form_token(),
        error=error,
        email=email,
        notice=notice,
    ), status


@app.route('/login')
@limiter.limit('10 per minute')
def login():
    """The one sign-in page: Google button + native email/password form.
    Every historical /login?next=...&invite=... link lands here; the endpoint
    name stays 'login' for auth._deny_unauthenticated's url_for."""
    next_target, invite = _login_page_args()
    notice = None
    if request.args.get('reset') == '1':
        notice = 'Password updated — sign in with your new password.'
    # The "approved right away" banner only for a CURRENTLY valid invite —
    # a dead token still rides the forms (harmless) but must not promise
    # an approval that redemption would fall back from.
    invite_valid = bool(invite and _valid_invite_for_redemption(invite))
    return _render_login(next_target, invite, notice=notice,
                         invite_valid=invite_valid)


@app.route('/login/google')
@limiter.limit('10 per minute')
def login_google():
    if not config.GOOGLE_CLIENT_ID:
        return 'Google login is not configured (set GOOGLE_CLIENT_ID).', 503
    # Remember where to land after the OAuth round-trip. Kept in the session
    # (not the state param) so state stays a pure CSRF token.
    session['login_next'] = auth._safe_next(request.args.get('next'), default='/log')
    # An invite token rides the session the same way (shape-checked only here;
    # real validation happens server-side in /callback). No/invalid param
    # clears any stale token from an earlier aborted flow.
    invite = request.args.get('invite')
    if invite and _INVITE_RE.match(invite):
        session['invite_token'] = invite
    else:
        session.pop('invite_token', None)
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    return redirect(auth.build_auth_url(state))


def _valid_invite_for_redemption(token, claimant=None):
    """(invite_row, inviter_row) when the token is redeemable, else None.

    ALL of: row exists, unrevoked, unused, unexpired, AND the inviter's row
    freshly re-read and still approved/admin — a rejected or deleted inviter
    must not keep minting approved accounts from links made earlier.

    claimant: a token already claimed BY THE SAME user stays redeemable —
    that's the retry of a signup that claimed but crashed before its account
    write (see db.claim_invite), not a second use."""
    if not token:
        return None
    invite = db.get_invite(token)
    if not invite:
        return None
    used_by = invite.get('used_by')
    if used_by is not None:
        if claimant is None or used_by != claimant:
            return None
    elif not db.invite_is_active(invite):
        return None
    inviter = db.get_user(invite['user_id'])
    if not inviter or inviter.get('status') not in auth.APPROVED_STATUSES:
        return None
    return invite, inviter


def _redeem_invite(token, new_user_id):
    """Validate + atomically claim an invite for new_user_id.

    Returns the inviter user_id on success, None otherwise (race losers and
    any error fall back to the normal pending flow — never an error page in
    the middle of the OAuth flow). Logs never include the token: pre-claim it
    is a live capability (invariant #8)."""
    try:
        valid = _valid_invite_for_redemption(token, claimant=new_user_id)
        if valid is None:
            return None
        invite, _ = valid
        if not db.claim_invite(token, new_user_id):
            return None  # lost the single-use race (or revoked/expired since)
        return invite['user_id']
    except Exception as e:
        print(f"Invite redemption failed for user {new_user_id}: {type(e).__name__}")
        return None


@app.route('/callback')
@limiter.limit('10 per minute')
def callback():
    # CSRF protection: the state must match what we issued in /login.
    if not request.args.get('state') or \
            request.args.get('state') != session.pop('oauth_state', None):
        return 'Invalid OAuth state', 400

    # Pop the invite token unconditionally so it can never survive into the
    # post-login session (session.clear() below is the backstop).
    invite_token = session.pop('invite_token', None)

    code = request.args.get('code')
    if not code:
        return redirect('/')

    info, err = auth.fetch_userinfo(code)
    if err:
        return err, 400

    user_id, email, name = info['sub'], info['email'], info['name']
    picture = info.get('picture', '')
    try:
        user = db.get_user(user_id)
        if user is None:
            # First sign-in. MAX_USERS is enforced HERE, server-side — BEFORE
            # any invite logic, so a full instance never consumes an invite
            # (the row stays usable once space frees up).
            if db.count_users() >= config.MAX_USERS:
                return render_template('full.html'), 403
            if email in config.ADMIN_EMAILS:
                # Admin bootstrap wins without consuming an invite.
                user = db.create_user(user_id, email, name, 'admin', picture)
            else:
                # An invite that validates AND is atomically claimed approves
                # the signup; any failure falls back to the normal pending
                # queue (the /i/ page filtered dead tokens before sign-in).
                inviter_id = _redeem_invite(invite_token, user_id)
                if inviter_id:
                    user = db.create_user(user_id, email, name, 'approved',
                                          picture, invited_by=inviter_id)
                else:
                    user = db.create_user(user_id, email, name, 'pending', picture)
        else:
            if (user.get('email') != email or user.get('name') != name
                    or user.get('picture', '') != picture):
                # sub is the stable key; keep email/name/picture current.
                db.update_user_profile(user_id, email, name, picture)
                user = {**user, 'email': email, 'name': name, 'picture': picture}
            if user.get('status') == 'pending':
                # A pending user redeeming an invite gets approved (friend
                # signed up first, then received the invite). The approval is
                # CONDITIONAL on still-pending so it can never overwrite a
                # concurrent admin rejection — rejection is a ban.
                inviter_id = _redeem_invite(invite_token, user_id)
                if inviter_id and db.approve_pending_user(user_id, inviter_id):
                    user = {**user, 'status': 'approved'}
    except Exception as e:
        print(f"Sign-in failed for user {user_id}: {type(e).__name__}")
        return 'Sign-in failed — please try again later.', 500

    status = user.get('status')
    if status not in auth.APPROVED_STATUSES and status != 'pending':
        # rejected: no session at all
        session.clear()
        return redirect('/')

    login_next = session.pop('login_next', None)
    session.clear()
    session['user_id'] = user_id
    session.permanent = True
    dest = auth._safe_next(login_next, default='/log')
    if status == 'pending':
        dest = '/waiting'
    return redirect(dest)


@app.route('/logout')
def logout():
    dest = auth._safe_next(request.args.get('next'))
    session.clear()
    return redirect(dest)


# --- Native accounts (email/password signup, verification, reset) ------------
# Anti-enumeration discipline mirrors the share/invite 404s: /signup always
# shows the same "check your email" page, sign-in failures share one exact
# error string (locked accounts included), /forgot always claims success, and
# every dead verify/reset link renders one shared page. The only accepted
# differentials: full.html at capacity (same as the Google path) and the
# "verify your email" page on a CORRECT password (which proves ownership).

_LOGIN_ERROR = 'Wrong email or password.'

# Auth side work (SES sends, the failure counter) runs OFF the response path:
# a synchronous SES call or DynamoDB write only on the account-exists branch
# would make response latency an account-existence oracle even with uniform
# bodies. Tests flip this off to run the work inline (deterministic asserts),
# like autolog.WORKER_ENABLED.
ASYNC_AUTH_WORK = True


def _defer(fn):
    """Run fn now (tests) or on a fire-and-forget daemon thread (prod).
    fn must not touch the request context — capture what it needs first."""
    if not ASYNC_AUTH_WORK:
        fn()
        return

    def run():
        try:
            fn()
        except Exception as e:
            print(f"AUTH_TASK_ERROR {type(e).__name__}")

    threading.Thread(target=run, daemon=True).start()


def _establish_session(user_id, status):
    """The post-auth session pattern (same as /callback's tail): clear first
    so no transient key survives, then user_id only. Returns the redirect."""
    if status not in auth.APPROVED_STATUSES and status != 'pending':
        session.clear()  # rejected: no session at all
        return redirect('/')
    session.clear()
    session['user_id'] = user_id
    session.permanent = True
    return None


def _dead_link():
    """The ONE page every dead verify/reset link renders (missing, expired,
    used — byte-identical, no oracle). login_next pinned to '/' so the token
    in the path never lands in the page's menu link."""
    return render_template('link_dead.html', user=None, login_next='/'), 404


def _send_verify_email(email, raw_token, base=None):
    link = f'{base or mailer.base_url()}/verify-email/{raw_token}'
    return mailer.send(
        email, 'Verify your email for Ndiro',
        'Welcome to Ndiro!\n\n'
        'Confirm your email address by opening this link:\n\n'
        f'  {link}\n\n'
        'The link is valid for 24 hours. If you did not sign up for Ndiro, '
        'ignore this email and nothing will happen.\n')


def _send_reset_email(email, raw_token, base=None):
    link = f'{base or mailer.base_url()}/reset/{raw_token}'
    return mailer.send(
        email, 'Reset your Ndiro password',
        'Someone asked to reset the password for your Ndiro account.\n\n'
        'Set a new password by opening this link:\n\n'
        f'  {link}\n\n'
        'The link is valid for 1 hour and can be used once. If this was not '
        'you, ignore this email — your password is unchanged.\n')


def _send_existing_account_email(email, provider, base=None):
    """Signup-attempt notice for an address that already has an account."""
    base = base or mailer.base_url()
    if provider == 'native':
        body = ('Someone tried to sign up for Ndiro with your email address, '
                'but you already have an account.\n\n'
                f'Sign in at {base}/login — or reset your password at '
                f'{base}/forgot if you have forgotten it.\n\n'
                'If this was not you, you can ignore this email.\n')
    else:
        body = ('Someone tried to sign up for Ndiro with your email address, '
                'but this address already has an account that signs in with '
                'Google.\n\n'
                f'Sign in with Google at {base}/login\n\n'
                'If this was not you, you can ignore this email.\n')
    return mailer.send(email, 'You already have an Ndiro account', body)


def _send_google_reset_notice(email, base):
    """Reset-request notice for a Google-only address — accurate copy, not
    the signup-attempt wording."""
    return mailer.send(
        email, 'About your Ndiro password reset',
        'Someone asked to reset the Ndiro password for this email address, '
        'but your account signs in with Google — it has no password to '
        'reset.\n\n'
        f'Sign in with Google at {base}/login\n\n'
        'If this was not you, you can ignore this email.\n')


def _check_email_page(email, sent_ok):
    """The uniform post-signup page — identical whether a row was created,
    the email already had an account, or the signup was a stale-row retry."""
    return render_template('check_email.html', user=None, email=email,
                           sent_ok=sent_ok, form_token=_form_token())


@app.route('/signup')
@limiter.limit('5 per minute')
def signup_page():
    if not mailer.enabled():
        return ('Email/password signup is not available on this instance '
                '(no outbound email is configured).', 503)
    next_target, invite = _login_page_args()
    # Non-consuming validation only — a dead invite renders the same signup
    # page without the banner (the /i/ page already 404s dead tokens).
    invite_valid = bool(invite and _valid_invite_for_redemption(invite))
    return render_template('signup.html', user=None,
                           next_target=next_target, invite=invite,
                           invite_valid=invite_valid,
                           form_token=_form_token())


@app.route('/signup', methods=['POST'])
@limiter.limit('5 per minute')
def signup_submit():
    if not mailer.enabled():
        return ('Email/password signup is not available on this instance '
                '(no outbound email is configured).', 503)
    if not _check_form_token():
        return 'Invalid form token — please reload the page and retry.', 400

    next_target, invite = _login_page_args()
    email, email_err = native_auth.valid_email(request.form.get('email'))
    password = request.form.get('password') or ''
    pw_err = native_auth.valid_password(password)
    name = (request.form.get('name') or '').strip()[:80]
    if email_err or pw_err:
        return render_template('signup.html', user=None,
                               next_target=next_target, invite=invite,
                               invite_valid=bool(invite and _valid_invite_for_redemption(invite)),
                               form_token=_form_token(),
                               error=email_err or pw_err,
                               email=request.form.get('email', ''),
                               name=name), 200

    try:
        # Hash BEFORE branching on duplicates: the scrypt work is the
        # dominant cost here, and skipping it on the already-registered
        # path would make response latency an account-existence oracle
        # despite the uniform page.
        password_hash = native_auth.hash_password(password)

        # Purge abandoned signups BEFORE the capacity gate: an expired
        # unverified PENDING native row is the one row class nobody can
        # free (no session, no owner), so left in place it could squat a
        # MAX_USERS slot forever. The delete is CONDITIONAL on the row
        # still being exactly what this scan observed — a resend that
        # refreshed the token or an admin rejection landing mid-request
        # wins, and the row then stays (and stays counted). Rejected rows
        # are deliberately never purged — rejection is a ban and must
        # stick, slot and all. A purged row never had a session, so it
        # owns no meals/photos/shares — the row delete is the whole wipe.
        now = time.time()
        rows = []
        for row in db.list_users():
            stale = (row.get('auth_provider') == 'native'
                     and not row.get('email_verified')
                     and row.get('status') == 'pending'
                     and int(row.get('verify_expires_at') or 0) < now)
            if stale and db.delete_stale_native_signup(
                    row['user_id'], int(row.get('verify_expires_at') or 0)):
                continue
            rows.append(row)

        # MAX_USERS before the duplicate check and before any invite logic
        # (invariant #9): a full instance never consumes an invite and
        # answers every signup identically.
        if len(rows) >= config.MAX_USERS:
            return render_template('full.html'), 403

        matches = [r for r in rows if (r.get('email') or '').lower() == email]
        if matches:
            # Same page as a fresh signup — no enumeration. The address
            # owner learns what happened by email instead.
            native = any(r.get('auth_provider') == 'native' for r in matches)
            sent = _send_existing_account_email(
                email, 'native' if native else 'google')
            return _check_email_page(email, sent)

        if invite and not _valid_invite_for_redemption(invite):
            invite = None  # dead invite: normal pending signup, never an error
        raw_token, token_hash = native_auth.mint_token()
        # name stays exactly as submitted — an empty name must NOT default
        # to the email local-part, which the invite/share attribution pages
        # would then display (they have their own anonymous fallbacks).
        created = db.create_native_user(
            native_auth.new_user_id(email), email, name,
            password_hash,
            token_hash, int(time.time()) + native_auth.VERIFY_TTL_S,
            pending_invite_token=invite)
        if created is None:
            # Lost a same-email signup race (the id is email-derived, so the
            # conditional put is the atomic uniqueness check): the winner's
            # owner just got a verification mail. Same uniform page.
            return _check_email_page(email, True)
    except Exception as e:
        print(f"Signup failed: {type(e).__name__}")
        return 'Signup failed — please try again later.', 500

    sent = _send_verify_email(email, raw_token)
    return _check_email_page(email, sent)


@app.route('/verify-email/<token>')
@limiter.limit('10 per minute')
def verify_email_page(token):
    """Non-consuming: email scanners prefetch GETs, and a prefetch must not
    burn the single-use token (or establish a session inside the scanner).
    The POST below does the actual work."""
    row = db.find_user_by_token_hash('verify_token_hash',
                                     native_auth.hash_token(token))
    if row is None or int(row.get('verify_expires_at') or 0) < time.time():
        return _dead_link()
    return render_template('verify_confirm.html', user=None, login_next='/',
                           token=token)


@app.route('/verify-email/<token>', methods=['POST'])
@limiter.limit('10 per minute')
def verify_email_submit(token):
    token_hash = native_auth.hash_token(token)
    try:
        row = db.find_user_by_token_hash('verify_token_hash', token_hash)
        if row is None or int(row.get('verify_expires_at') or 0) < time.time():
            return _dead_link()
        user_id = row['user_id']
        status = row.get('status')
        # Order: claim invite -> approve -> ONLY THEN consume the verify
        # token. Every crash window then retries cleanly through the same
        # link: claim_invite is idempotent for the same claimant, approve is
        # conditional on still-pending (a retry of a half-finished attempt
        # reads the already-approved status off the row). Consuming the
        # token first could burn the invite AND the link with the account
        # still pending — unrecoverable.
        pending_invite = row.get('pending_invite_token')
        if pending_invite:
            inviter_id = _redeem_invite(pending_invite, user_id)
            if inviter_id and db.approve_pending_user(user_id, inviter_id):
                status = 'approved'
        if not db.mark_email_verified(user_id, token_hash):
            return _dead_link()  # lost a double-submit race: link already used
    except Exception as e:
        print(f"Email verification failed: {type(e).__name__}")
        return 'Verification failed — please try again later.', 500

    deny = _establish_session(user_id, status)
    if deny is not None:
        return deny
    return redirect('/waiting' if status == 'pending' else '/log')


@app.route('/resend-verification', methods=['POST'])
@limiter.limit('3 per minute')
def resend_verification():
    if not mailer.enabled():
        return ('Email is not configured on this instance.', 503)
    if not _check_form_token():
        return 'Invalid form token — please reload the page and retry.', 400
    email, err = native_auth.valid_email(request.form.get('email'))
    if err is None:
        # ALL the account-dependent work (lookup, re-mint, SES call) runs off
        # the response path so response timing is identical whether or not
        # the address has an unverified account.
        base = mailer.base_url()  # request-bound: capture before deferring
        _defer(lambda: _resend_verification_work(email, base))
    # Uniform regardless of whether anything was sent — no enumeration.
    return _check_email_page(email or (request.form.get('email') or '').strip(),
                             True)


def _resend_verification_work(email, base):
    try:
        row = db.find_user_by_email(email, provider='native')
        if row is not None and not row.get('email_verified'):
            raw_token, token_hash = native_auth.mint_token()
            db.set_verify_token(row['user_id'], token_hash,
                                int(time.time()) + native_auth.VERIFY_TTL_S)
            _send_verify_email(email, raw_token, base)
    except Exception as e:
        print(f"Resend verification failed: {type(e).__name__}")


@app.route('/login/password', methods=['POST'])
@limiter.limit('10 per minute')
def login_password():
    if not _check_form_token():
        return 'Invalid form token — please reload the page and retry.', 400
    next_target, invite = _login_page_args()
    email, email_err = native_auth.valid_email(request.form.get('email'))
    password = request.form.get('password') or ''
    fail = lambda: _render_login(next_target, invite, error=_LOGIN_ERROR,
                                 email=request.form.get('email', ''))
    if email_err:
        native_auth.verify_dummy(password)
        return fail()

    try:
        user = db.find_user_by_email(email, provider='native')
        if user is None:
            native_auth.verify_dummy(password)  # flatten the timing
            return fail()
        if native_auth.is_locked(user):
            # Deliberately no distinct message (an account-existence oracle)
            # and no REAL hash check (the lockout must actually stop
            # guessing) — but still burn the dummy hash so a locked account
            # doesn't answer measurably faster than a wrong password.
            # "Forgot password?" is the way out — reset clears the lockout.
            native_auth.verify_dummy(password)
            return fail()
        if not native_auth.verify_password(user.get('password_hash', ''), password):
            # Deferred: the counter write must not make wrong-password
            # responses measurably slower than unknown-email ones.
            uid = user['user_id']
            _defer(lambda: db.record_login_failure(
                uid, native_auth.LOCKOUT_THRESHOLD, native_auth.LOCKOUT_S))
            return fail()
        if not user.get('email_verified'):
            # Correct password proves ownership — pointing at verification
            # discloses nothing the owner doesn't know.
            return render_template('verify_needed.html', user=None,
                                   email=email, form_token=_form_token())
        db.clear_login_failures(user['user_id'])
        status = user.get('status')
        # Google-flow parity (see /callback): a pending user signing in
        # through an invite link gets approved — conditionally, so a
        # concurrent admin rejection always wins.
        if status == 'pending' and invite:
            inviter_id = _redeem_invite(invite, user['user_id'])
            if inviter_id and db.approve_pending_user(user['user_id'], inviter_id):
                status = 'approved'
    except Exception as e:
        print(f"Password sign-in failed: {type(e).__name__}")
        return 'Sign-in failed — please try again later.', 500

    deny = _establish_session(user['user_id'], status)
    if deny is not None:
        return deny
    if status == 'pending':
        return redirect('/waiting')
    return redirect(next_target)


@app.route('/forgot')
@limiter.limit('10 per minute')
def forgot_page():
    if not mailer.enabled():
        return ('Password reset is not available on this instance '
                '(no outbound email is configured).', 503)
    return render_template('forgot.html', user=None, form_token=_form_token())


@app.route('/forgot', methods=['POST'])
@limiter.limit('3 per minute')
def forgot_submit():
    if not mailer.enabled():
        return ('Password reset is not available on this instance '
                '(no outbound email is configured).', 503)
    if not _check_form_token():
        return 'Invalid form token — please reload the page and retry.', 400
    email, err = native_auth.valid_email(request.form.get('email'))
    if err is None:
        # ALL the account-dependent work (lookups, token mint, SES call)
        # runs off the response path: a synchronous SES call only on the
        # account-exists branches would make latency an enumeration oracle
        # even though the page below is uniform.
        base = mailer.base_url()  # request-bound: capture before deferring
        _defer(lambda: _forgot_work(email, base))
    # Uniform for every input — unknown address included. No enumeration.
    return render_template('forgot_sent.html', user=None)


def _forgot_work(email, base):
    try:
        # Prefer the NATIVE row: a Google and a native account may share
        # an email, and only the native one has a password to reset —
        # picking whichever scan row came first could strand it.
        user = (db.find_user_by_email(email, provider='native')
                or db.find_user_by_email(email))
        if user is None:
            pass  # nothing to send; the page already claimed success
        elif user.get('auth_provider') != 'native':
            _send_google_reset_notice(email, base)
        elif not user.get('email_verified'):
            # Resetting the password wouldn't let them in; verification is
            # the actual blocker (and the mail proves inbox control).
            raw_token, token_hash = native_auth.mint_token()
            db.set_verify_token(user['user_id'], token_hash,
                                int(time.time()) + native_auth.VERIFY_TTL_S)
            _send_verify_email(email, raw_token, base)
        else:
            raw_token, token_hash = native_auth.mint_token()
            db.set_reset_token(user['user_id'], token_hash,
                               int(time.time()) + native_auth.RESET_TTL_S)
            _send_reset_email(email, raw_token, base)
    except Exception as e:
        print(f"Password-reset request failed: {type(e).__name__}")


@app.route('/reset/<token>')
@limiter.limit('10 per minute')
def reset_page(token):
    row = db.find_user_by_token_hash('reset_token_hash',
                                     native_auth.hash_token(token))
    if row is None or int(row.get('reset_expires_at') or 0) < time.time():
        return _dead_link()
    return render_template('reset.html', user=None, login_next='/',
                           token=token)


@app.route('/reset/<token>', methods=['POST'])
@limiter.limit('10 per minute')
def reset_submit(token):
    token_hash = native_auth.hash_token(token)
    try:
        row = db.find_user_by_token_hash('reset_token_hash', token_hash)
        if row is None or int(row.get('reset_expires_at') or 0) < time.time():
            return _dead_link()
        password = request.form.get('password') or ''
        pw_err = native_auth.valid_password(password)
        if pw_err is None and password != (request.form.get('password2') or ''):
            pw_err = 'The two passwords do not match.'
        if pw_err:
            return render_template('reset.html', user=None, login_next='/',
                                   token=token, error=pw_err), 200
        if not db.complete_password_reset(
                row['user_id'], native_auth.hash_password(password), token_hash):
            return _dead_link()  # link already used
    except Exception as e:
        print(f"Password reset failed: {type(e).__name__}")
        return 'Password reset failed — please try again later.', 500
    # No session from a reset link: sign in with the new password (keeps
    # session establishment to the three sign-in points).
    return redirect('/login?reset=1')


@app.route('/api/settings/password', methods=['POST'])
@limiter.limit('10 per minute')  # scrypt x2 per call — keep it off the global lane
@auth.approved_required
def change_password():
    """Signed-in password change (native accounts only). Requires the current
    password so a walked-away-from session can't silently take the account.
    Completing it also invalidates any outstanding reset link (db side)."""
    if g.user.get('auth_provider') != 'native':
        return jsonify({'error': 'This account signs in with Google.'}), 400
    data = request.get_json(silent=True) or {}
    current = data.get('current') or ''
    new = data.get('new') or ''
    if not native_auth.verify_password(g.user.get('password_hash', ''), current):
        return jsonify({'error': 'Current password is incorrect.'}), 400
    pw_err = native_auth.valid_password(new)
    if pw_err:
        return jsonify({'error': pw_err}), 400
    try:
        db.set_password_hash(g.user['user_id'], native_auth.hash_password(new))
    except Exception as e:
        print(f"Password change failed for user {g.user['user_id']}: {type(e).__name__}")
        return jsonify({'error': 'Failed to save — please try again'}), 500
    return jsonify({'ok': True})


# --- Admin API (account metadata ONLY — never another user's meals/photos) ---

def _user_to_json(u, email_by_id=None):
    """Admin-facing view of a user row: ACCOUNT METADATA ONLY.

    Deliberately no usage figures — not meal or photo counts, and not the AI
    daily counter either. What one person did is theirs; /privacy promises an
    admin sees who has an account, not what they do with it. Instance-wide AI
    use (and how many accounts are at the cap) is on /admin/monitor, where it
    names nobody.

    email_by_id: user_id -> email lookup so 'invited by' shows something
    readable; a deleted inviter resolves to null (UI shows a fallback).
    """
    return {
        'user_id': u.get('user_id'),
        'email': u.get('email'),
        'name': u.get('name'),
        'status': u.get('status'),
        'created_at': u.get('created_at'),
        'approved_at': u.get('approved_at'),
        'invited_by': u.get('invited_by'),
        'invited_by_email': (email_by_id or {}).get(u.get('invited_by')),
        # Derived boolean only — the explicit allowlist above is what keeps
        # password/token hashes out of the payload. True marks a native
        # signup that never clicked its verification link (explains a stuck
        # pending row; the admin can reject it to free the slot).
        'unverified': bool(u.get('auth_provider') == 'native'
                           and not u.get('email_verified')),
    }


@app.route('/api/admin/users')
@auth.admin_required
def admin_list_users():
    try:
        users = db.list_users()
    except Exception as e:
        print(f"Error listing users: {type(e).__name__}")
        return jsonify({'error': 'Failed to list users'}), 500
    users.sort(key=lambda u: u.get('created_at') or '')
    # Resolve "invited by" from the rows already in hand — no extra queries.
    email_by_id = {u.get('user_id'): u.get('email') for u in users}
    return jsonify({'users': [_user_to_json(u, email_by_id) for u in users],
                    'max_users': config.MAX_USERS})


@app.route('/api/admin/users/<user_id>/<action>', methods=['POST'])
@auth.admin_required
def admin_set_status(user_id, action):
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'Unknown action'}), 400
    try:
        target = db.get_user(user_id)
    except Exception as e:
        print(f"Error fetching user: {type(e).__name__}")
        return jsonify({'error': 'Failed to fetch user'}), 500
    if target is None:
        return jsonify({'error': 'User not found'}), 404
    if target.get('status') == 'admin':
        return jsonify({'error': 'Admins cannot be modified here'}), 400
    status = 'approved' if action == 'approve' else 'rejected'
    try:
        db.set_user_status(user_id, status)
    except Exception as e:
        print(f"Error setting user status: {type(e).__name__}")
        return jsonify({'error': 'Failed to update user'}), 500
    return jsonify({'user_id': user_id, 'status': status})


# --- Admin monitoring --------------------------------------------------------
# Instance health in numbers: accounts, meals, photos, links. INSTANCE-WIDE
# AGGREGATES ONLY — nothing here is attributable to an individual account.
# /admin stays the only place a user's row is shown, and it shows the metadata
# it always has (email, name, status); this route adds NO per-account figure to
# that, deliberately. Two things hold it there:
#   - db.py groups by user_id internally (orphan detection and the "how many
#     accounts are active" counts need it), but no per-user key, count, or date
#     is ever serialized — only sums and cardinalities.
#   - the meals scan is projected down to (user_id, date), so no description,
#     context, or nutrient value reaches this code even by accident, and photos
#     are counted from S3 key listings without fetching a single byte.
# tests/test_m10_monitor.py asserts both. Do not add a per-account field here.

@app.route('/admin/monitor')
@auth.admin_required
def monitor_page():
    return render_template('monitor.html', user=g.user)


def _window_start(anchor_str, days):
    """Inclusive start of a days-long window ending on anchor_str.

    Windows are closed at BOTH ends ([start, anchor]) because meal dates are
    the client's own (invariant #10): an unbounded window would make
    "last 7 days" quietly include everything logged after it. A meal dated
    ahead of the admin's day — a user a timezone ahead — therefore sits
    outside the window until the admin's own date catches up."""
    return (date.fromisoformat(anchor_str) - timedelta(days=days - 1)).isoformat()


def _section(label, fn):
    """Run one stats collector. A failing section degrades to None so the rest
    of the dashboard still renders — a monitoring page that 500s the moment S3
    hiccups is exactly the page you cannot use during an incident."""
    try:
        return fn()
    except Exception as e:
        print(f"Monitor: {label} stats unavailable: {type(e).__name__}")
        return None


@app.route('/api/admin/stats')
@limiter.limit('12 per minute')  # each call is a full scan of every table — cheap, not free
@auth.admin_required
def admin_stats():
    """Instance-wide usage counters for the monitoring page.

    ?anchor=YYYY-MM-DD is the admin's local today (invariant #10): meal dates
    are client-local, so the 7/30-day windows must be measured against a
    client-supplied day, not the server clock. UTC is the fallback.
    """
    anchor = _valid_date(request.args.get('anchor', '')) or _utc_today_str()
    utc_today = _utc_today_str()
    day7, day30 = _window_start(anchor, 7), _window_start(anchor, 30)
    utc7, utc30 = _window_start(utc_today, 7), _window_start(utc_today, 30)

    users = _section('user', db.list_users)
    meals = _section('meal', db.scan_meal_stats)
    photos = _section('photo', db.scan_photo_stats)
    shares = _section('share', db.scan_share_stats)
    invites = _section('invite', db.scan_invite_stats)
    if users is None:
        return jsonify({'error': 'Failed to load instance stats'}), 500

    by_status = {}
    for u in users:
        status = u.get('status') or 'unknown'
        by_status[status] = by_status.get(status, 0) + 1
    ai_rows = [u for u in users if u.get('ai_uses_date') == utc_today]

    accounts = {
        'total': len(users),
        'max': config.MAX_USERS,
        'by_status': by_status,
        # created_at is a SERVER UTC stamp, so its window is UTC-based — the
        # client anchor governs meal dates only (invariant #10 cuts both ways).
        'new_7d': sum(1 for u in users if (u.get('created_at') or '')[:10] >= utc7),
        'new_30d': sum(1 for u in users if (u.get('created_at') or '')[:10] >= utc30),
        'invited': sum(1 for u in users if u.get('invited_by')),
        'ai_uses_today': sum(int(u.get('ai_uses_today', 0)) for u in ai_rows),
        'ai_users_today': sum(1 for u in ai_rows if int(u.get('ai_uses_today', 0))),
        'ai_at_cap': sum(1 for u in ai_rows
                         if int(u.get('ai_uses_today', 0)) >= config.AI_DAILY_LIMIT),
    }

    meals_out = None
    if meals is not None:
        per_user = meals['per_user']
        meals_out = {
            'total': meals['total'],
            'truncated': meals['truncated'],
            'days_logged': sum(r['days'] for r in per_user.values()),
            'logged_7d': sum(n for d, n in meals['by_date'].items() if day7 <= d <= anchor),
            'logged_30d': sum(n for d, n in meals['by_date'].items() if day30 <= d <= anchor),
            'active_7d': sum(1 for r in per_user.values() if day7 <= (r['last'] or '') <= anchor),
            'active_30d': sum(1 for r in per_user.values() if day30 <= (r['last'] or '') <= anchor),
            'logging_accounts': len(per_user),
        }

    # Rows keyed by a user_id with no users-table row: normally zero. A
    # non-zero count means an account deletion stopped part-way (photos ->
    # meals -> shares -> invites -> user row) and left data behind.
    known = {u.get('user_id') for u in users}
    orphans = {
        'meals': sum(r['meals'] for uid, r in (meals or {}).get('per_user', {}).items()
                     if uid not in known),
        'photos': sum(r['photos'] for uid, r in (photos or {}).get('per_user', {}).items()
                      if uid not in known),
    } if meals is not None and photos is not None else None

    return jsonify({
        'anchor': anchor,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'instance': {
            'commit': config.GIT_COMMIT_SHORT,
            'branch': config.GIT_BRANCH,
            'uptime': _uptime_str(time.time() - config.STARTED_AT),
            'ai_enabled': bool(config.OPENAI_API_KEY),
            'ai_daily_limit': config.AI_DAILY_LIMIT,
            'photos_enabled': bool(config.S3_BUCKET),
        },
        'accounts': accounts,
        'meals': meals_out,
        'photos': photos and {k: v for k, v in photos.items() if k != 'per_user'},
        'shares': shares,
        'invites': invites,
        'orphans': orphans,
    })


# --- Meal log ----------------------------------------------------------------
# Tenant discipline: every handler keys on g.user['user_id'] from the session.
# user_id NEVER comes from the URL, query string, or form.

@app.route('/log')
@auth.approved_required
def log_page():
    # Kick the auto-log worker: after a restart this is what resumes any
    # spooled-but-unprocessed batch photos without waiting for a new upload.
    autolog.ensure_worker()
    return render_template('log.html', user=g.user,
                           nutrient=config.resolve_nutrient(g.user),
                           fiber_guide=config.FIBER_GUIDE,
                           ai_enabled=bool(config.OPENAI_API_KEY),
                           # Auto-add needs BOTH the estimator and photo storage.
                           photos_enabled=bool(config.S3_BUCKET))


def _valid_date(date_str):
    """Return the string if it is a strictly canonical YYYY-MM-DD date, else None.

    strptime alone is lenient ('2026-8-5' parses), which would produce a sort
    key that zero-padded month queries never match (silent data loss) and 500
    when fed to date.fromisoformat as an anchor. Require the round trip.

    Callers also do +/-N-day arithmetic on an accepted date (the day list in
    _meals_payload, the windows in /api/admin/stats), which overflows within a
    month of date.min/date.max. That is this function's job to prevent, so the
    slack is checked here rather than at every call site.
    """
    if not isinstance(date_str, str) or not _DATE_RE.match(date_str):
        return None
    try:
        parsed = datetime.strptime(date_str, '%Y-%m-%d').date()
        parsed - _DATE_SLACK
        parsed + _DATE_SLACK
        return date_str
    except (ValueError, OverflowError):
        return None


def _valid_month(month_str):
    """Parse a strictly canonical YYYY-MM into (year, month), else None."""
    if not isinstance(month_str, str) or not _MONTH_RE.match(month_str):
        return None
    try:
        parsed = datetime.strptime(month_str, '%Y-%m')
        return parsed.year, parsed.month
    except ValueError:
        return None


def _nutrients_from_form(form, nutrient_key):
    """Parse nutrient form fields into a DynamoDB-ready map of Decimals.

    The single field name IS the user's resolved nutrient key ('fiber_g' by
    default) — form field and storage key are the same string. Empty string
    means "unset"; the totals/JSON plumbing is key-generic already.
    """
    nutrients = {}
    for field in (nutrient_key,):
        raw = (form.get(field) or '').strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise ValueError(f'{field} must be a number')
        # Decimal('NaN'/'Infinity') parse fine but blow up later: NaN raises
        # InvalidOperation on comparison, Infinity is rejected by DynamoDB.
        if not value.is_finite():
            raise ValueError(f'{field} must be a real number')
        if value < 0:
            raise ValueError(f'{field} must be >= 0')
        # Sane upper bound: also keeps huge finite values (e.g. 1e999) from
        # reaching DynamoDB, which rejects them (a 500) — no real meal is 100kg.
        if value > _NUTRIENT_MAX:
            raise ValueError(f'{field} is implausibly large')
        nutrients[field] = value
    return nutrients


def _stale_nutrient_form(form, nutrient_key):
    """400 response when the page that posted this form was rendered under a
    different tracked micro (its hidden nutrient_key disagrees) — otherwise a
    stale tab's typed amount would be dropped silently. Absent field = older
    client; accept."""
    sent = form.get('nutrient_key')
    if sent and sent != nutrient_key:
        return jsonify({'error': 'Your tracked micro changed — reload this '
                                 'page and try again'}), 400
    return None


def _read_meal_form(form, nutrient_key):
    """Validate the fields shared by add/edit. Returns (description, context,
    nutrients); raises ValueError(message) on any invalid field."""
    description = (form.get('description') or '').strip()
    if not description:
        raise ValueError('Description is required')
    if len(description) > 500:
        raise ValueError('Description too long (max 500 characters)')
    context = (form.get('context') or '').strip()
    if len(context) > 500:
        raise ValueError('Context too long (max 500 characters)')
    return description, context, _nutrients_from_form(form, nutrient_key)


def _assemble_meal_item(user_id, date_str, meal_id, description, context,
                        nutrients, photo_key, created_at, form,
                        photo_changed=False, prev_photo_v=None):
    """Build a meal item dict (shared by add/edit).

    photo_v stamps WHEN the photo bytes last changed — it drives the photo
    proxy's cache version, so it must move only on upload/replace
    (photo_changed), never on text edits (prev_photo_v carries over)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    item = {
        'user_id': user_id,
        'sk': db.meal_sk(date_str, meal_id),
        'date': date_str,
        'meal_id': meal_id,
        'description': description,
        'nutrients': nutrients,
        'created_at': created_at or now_iso,
        'updated_at': now_iso,
    }
    if context:
        item['context'] = context
    if form.get('ai_assisted') == '1':
        item['ai_assisted'] = True
    if photo_key:
        item['photo_key'] = photo_key
        if photo_changed:
            item['photo_v'] = now_iso
        elif prev_photo_v:
            item['photo_v'] = prev_photo_v
    return item


def _photo_version(item):
    """Photo cache/ETag version: digest of photo_v — a timestamp written ONLY
    when the photo bytes change (upload/replace), so text-only meal edits do
    NOT bust browser/LRU caches. Legacy rows (pre-photo_v) fall back to
    created_at, which is equally stable across edits; their next photo
    replace writes photo_v and takes over."""
    stamp = item.get('photo_v') or item.get('created_at') or ''
    return hashlib.sha1(stamp.encode()).hexdigest()[:16]


def _meal_to_json(item, photo_base='/photo'):
    """DynamoDB meal item -> JSON-safe dict (Decimals become floats).
    Photo URLs point at the authenticated proxy (photo_base picks the
    audience: '/photo' for the owner, '/s/<token>/photo' for share views);
    ?v= is a cache-buster only — the route re-derives it server-side."""
    photo_key = item.get('photo_key')
    meal_id = item['meal_id']
    return {
        'meal_id': meal_id,
        'date': item['date'],
        # Meal time lives in the meal_id's HHMMSS prefix (drives ordering).
        'time': f'{meal_id[0:2]}:{meal_id[2:4]}' if meal_id[:6].isdigit() else None,
        'description': item.get('description', ''),
        'context': item.get('context', ''),
        'ai_assisted': bool(item.get('ai_assisted')),
        'nutrients': {k: float(v) for k, v in (item.get('nutrients') or {}).items()},
        'has_photo': bool(photo_key),
        # No URL without a bucket: the proxy could never resolve it, and a
        # broken-image icon per meal is worse than no image (S3_BUCKET is
        # documented as optional).
        'photo_url': (f"{photo_base}/{item['date']}/{meal_id}?v={_photo_version(item)}"
                      if photo_key and config.S3_BUCKET else None),
        'created_at': item.get('created_at'),
        'updated_at': item.get('updated_at'),
    }


def _day_totals(meals_json):
    """Sum every nutrient key across a day's meals (generic — no per-key code)."""
    totals = {}
    for meal in meals_json:
        for key, value in meal['nutrients'].items():
            totals[key] = round(totals.get(key, 0) + value, 2)
    return totals


def _meals_payload(user_id, args, photo_base='/photo'):
    """Build the meals payload for one user: a single date (?date=), a month
    (?month=YYYY-MM), or the last N days (?days=N, default 7, max 31).
    photo_base picks the photo-proxy audience ('/photo' or '/s/<token>/photo').

    ?anchor=YYYY-MM-DD is the CLIENT's local today — it bounds future-month
    validation and sets the payload's `today`. The server clock (UTC) is never
    used for user-local dates; it is only the fallback window end when no
    anchor is sent. Empty day entries are included for the whole requested
    range (the review chart needs the full month axis), days newest-first,
    meals chronological within a day (the sk sorts them).

    Returns (payload_dict, http_status).
    """
    anchor = args.get('anchor')
    if anchor and not _valid_date(anchor):
        return {'error': 'Invalid anchor format. Use YYYY-MM-DD'}, 400
    today = date.fromisoformat(anchor) if anchor \
        else datetime.now(timezone.utc).date()

    single_date = args.get('date')
    if single_date and not _valid_date(single_date):
        return {'error': 'Invalid date format. Use YYYY-MM-DD'}, 400
    month = args.get('month')

    error = None
    if single_date:
        dates = [single_date]
        fetch = lambda: db.query_meals_day(user_id, single_date)
    elif month:
        ym = _valid_month(month)
        if not ym:
            return {'error': 'Invalid month format. Use YYYY-MM'}, 400
        year, mon = ym
        first = date(year, mon, 1)
        if first > today:
            return {'error': 'month is in the future'}, 400
        # Full month, future days within the current month excluded.
        last = min(date(year, mon, calendar.monthrange(year, mon)[1]), today)
        dates = [(first + timedelta(days=i)).isoformat()
                 for i in range((last - first).days, -1, -1)]
        fetch = lambda: db.query_meals_month(user_id, month)
    else:
        try:
            n_days = min(max(int(args.get('days', 7)), 1), 31)
        except ValueError:
            return {'error': 'days must be an integer'}, 400
        dates = [(today - timedelta(days=i)).isoformat() for i in range(n_days)]
        fetch = lambda: db.query_meals_range(user_id, dates[-1], dates[0])

    # ONE Query per request (already chronological); group by date server-side.
    by_date = {d: [] for d in dates}
    try:
        for item in fetch():
            if item.get('date') in by_date:
                by_date[item['date']].append(item)
    except Exception as e:
        # Degrade gracefully (e.g. table not created yet): page still renders.
        print(f"Error querying meals: {type(e).__name__}")
        error = 'meals unavailable'

    days = []
    for d in dates:
        meals = [_meal_to_json(item, photo_base) for item in by_date[d]]
        days.append({'date': d, 'totals': _day_totals(meals), 'meals': meals})

    payload = {'today': today.isoformat(), 'days': days}
    if error:
        payload['error'] = error
    return payload, 200


@app.route('/api/meals')
@auth.approved_required
def get_meals():
    payload, status = _meals_payload(g.user['user_id'], request.args)
    return jsonify(payload), status


@app.route('/api/meals', methods=['POST'])
@auth.approved_required
def add_meal():
    """Create a meal (multipart form: description, date required, optional
    context/time/photo/ai_assisted, plus the user's nutrient field —
    'fiber_g' by default)."""
    user_id = g.user['user_id']

    # The DATE is user-local and must come from the client — the server clock
    # is never a fallback for it (a UTC server is up to a day off the user).
    date_str = request.form.get('date')
    if not date_str:
        return jsonify({'error': 'Date is required'}), 400
    if not _valid_date(date_str):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    # Reject future dates: a meal dated ahead of "today" is filtered out of
    # every view, so it would look like a failed save. 1-day slack covers a
    # client whose local date is ahead of the UTC server.
    if date.fromisoformat(date_str) > datetime.now(timezone.utc).date() + timedelta(days=1):
        return jsonify({'error': "Date can't be in the future"}), 400

    nutrient_key = config.resolve_nutrient(g.user)['key']
    stale = _stale_nutrient_form(request.form, nutrient_key)
    if stale:
        return stale
    try:
        description, context, nutrients = _read_meal_form(request.form, nutrient_key)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # HHMMSS prefix keeps meals chronological within the day; hex suffix for
    # uniqueness. Client normally sends its local time; UTC now is the
    # fallback for the TIME only, never the date.
    time_str = (request.form.get('time') or '').strip()
    if time_str:
        try:
            meal_time = datetime.strptime(time_str, '%H:%M')
        except ValueError:
            return jsonify({'error': 'Invalid time format. Use HH:MM'}), 400
        meal_id = f"{meal_time:%H%M}00-{uuid4().hex[:6]}"
    else:
        meal_id = f"{datetime.now(timezone.utc):%H%M%S}-{uuid4().hex[:6]}"

    photo = request.files.get('photo')
    photo_key = None
    if photo and photo.filename:
        if not config.S3_BUCKET:
            return jsonify({'error': 'Photo storage not configured (set S3_BUCKET)'}), 400
        # Key built server-side only: resolved user + validated date + fresh id.
        photo_key = db.photo_key(user_id, date_str, meal_id)
        try:
            db.put_photo(photo, photo_key)
        except ValueError:
            return jsonify({'error': "Couldn't read that image — try a JPEG or PNG"}), 400
        except Exception as e:
            print(f"Error uploading photo for user {user_id}: {type(e).__name__}")
            return jsonify({'error': 'Photo upload failed'}), 502

    item = _assemble_meal_item(user_id, date_str, meal_id, description, context,
                               nutrients, photo_key, None, request.form,
                               photo_changed=bool(photo_key))

    try:
        db.put_meal(item)
    except Exception as e:
        print(f"Error saving meal for user {user_id}: {type(e).__name__}")
        if photo_key:
            db.delete_photo(photo_key)  # don't orphan the just-uploaded object
        return jsonify({'error': 'Failed to save meal'}), 500

    return jsonify(_meal_to_json(item)), 201


@app.route('/api/meals/<date_str>/<meal_id>', methods=['PUT'])
@auth.approved_required
def update_meal(date_str, meal_id):
    """Edit a meal: description/context/nutrients, replace photo, or
    remove_photo=1. Key = (session user, date#meal_id) — date/time can't move."""
    user_id = g.user['user_id']
    if not _valid_date(date_str):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    try:
        existing = db.get_meal(user_id, date_str, meal_id)
    except Exception as e:
        print(f"Error fetching meal for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to fetch meal'}), 500
    if not existing:
        return jsonify({'error': 'Meal not found'}), 404

    nutrient_key = config.resolve_nutrient(g.user)['key']
    stale = _stale_nutrient_form(request.form, nutrient_key)
    if stale:
        return stale
    try:
        description, context, nutrients = _read_meal_form(request.form, nutrient_key)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Preserve values stored under OTHER nutrient keys (meals logged before a
    # micro switch): the form only carries the active key, and a plain replace
    # would erase the old value the settings page promises to keep.
    for k, v in (existing.get('nutrients') or {}).items():
        if k != nutrient_key:
            nutrients[k] = v

    photo_key = existing.get('photo_key')
    # Defer the S3 removal until the row is safely written, so a failed update
    # never leaves the still-current row pointing at a deleted object.
    delete_after_commit = None
    if request.form.get('remove_photo') and photo_key:
        delete_after_commit = photo_key
        photo_key = None

    photo = request.files.get('photo')
    if photo and photo.filename:
        if not config.S3_BUCKET:
            return jsonify({'error': 'Photo storage not configured (set S3_BUCKET)'}), 400
        # Reuse the meal's canonical key so a replacement overwrites in place
        # (no delete, no orphan). Overrides a pending remove_photo.
        photo_key = db.photo_key(user_id, date_str, meal_id)
        delete_after_commit = None
        try:
            db.put_photo(photo, photo_key)
        except ValueError:
            return jsonify({'error': "Couldn't read that image — try a JPEG or PNG"}), 400
        except Exception as e:
            print(f"Error uploading photo for user {user_id}: {type(e).__name__}")
            return jsonify({'error': 'Photo upload failed'}), 502

    item = _assemble_meal_item(user_id, date_str, meal_id, description, context,
                               nutrients, photo_key, existing.get('created_at'),
                               request.form,
                               photo_changed=bool(photo and photo.filename),
                               prev_photo_v=existing.get('photo_v'))

    try:
        db.put_meal(item)
    except Exception as e:
        print(f"Error updating meal for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to update meal'}), 500

    if delete_after_commit:
        db.delete_photo(delete_after_commit)  # best-effort, post-commit
    return jsonify(_meal_to_json(item))


@app.route('/api/meals/<date_str>/<meal_id>', methods=['DELETE'])
@auth.approved_required
def delete_meal(date_str, meal_id):
    """Delete a meal and (best-effort) its photo."""
    user_id = g.user['user_id']
    try:
        existing = db.get_meal(user_id, date_str, meal_id)
    except Exception as e:
        print(f"Error fetching meal for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to fetch meal'}), 500
    if not existing:
        return jsonify({'error': 'Meal not found'}), 404

    # Delete the row first: if that fails we return an error with the photo
    # still intact (not a row pointing at a missing object).
    try:
        db.delete_meal_item(user_id, date_str, meal_id)
    except Exception as e:
        print(f"Error deleting meal for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to delete meal'}), 500

    db.delete_photo(existing.get('photo_key'))  # best-effort, post-commit
    return jsonify({'deleted': True, 'date': date_str, 'meal_id': meal_id})


# --- Photo proxy -------------------------------------------------------------
# Photos are served THROUGH the app (in-process LRU + browser caching via
# versioned URLs) instead of presigned S3 links. Auth is the meal-row read:
# owner routes key on the session user_id, share routes on the token row —
# the S3 key is rebuilt server-side either way, never taken from URL or row.

def _serve_photo(owner_user_id, date_str, meal_id, not_found, cache_control):
    """Shared photo responder. not_found: zero-arg callable returning the
    audience's exact 404 (owner vs share bodies differ; each audience's dead
    states are byte-identical). The version/ETag is re-derived from the meal
    row that also proves authorization; a ?v= that DISAGREES with it 404s —
    an immutable URL must never serve bytes other than its own version."""
    if not _valid_date(date_str):
        return not_found()
    try:
        item = db.get_meal(owner_user_id, date_str, meal_id)
    except Exception as e:
        print(f"Error fetching meal for photo (user {owner_user_id}): {type(e).__name__}")
        return jsonify({'error': 'Photo unavailable'}), 502
    if not item or not item.get('photo_key'):
        return not_found()
    etag = _photo_version(item)
    if request.args.get('v') not in (None, '', etag):
        return not_found()  # stale version URL: dead, never "the current bytes"
    # Weak comparison per RFC 7232: intermediaries (Cloudflare fronts this
    # app) may downgrade ETags to W/"..." and clients echo that form back;
    # strong contains() would miss and re-send the full image every day.
    if request.if_none_match.contains_weak(etag):
        resp = app.response_class(status=304)  # no S3, no cache touch
    else:
        key = db.photo_key(owner_user_id, date_str, meal_id)
        try:
            data = db.get_photo_bytes(key, owner_user_id, etag)
        except Exception as e:
            print(f"Error fetching photo bytes (user {owner_user_id}): {type(e).__name__}")
            return jsonify({'error': 'Photo unavailable'}), 502
        if data is None:
            return not_found()
        resp = app.response_class(data, mimetype='image/jpeg')
    resp.set_etag(etag)
    resp.headers['Cache-Control'] = cache_control
    return resp


@app.route('/photo/<date_str>/<meal_id>')
@limiter.limit('600 per minute')  # per-image route: photo-heavy month views (100+ imgs) + shared-NAT households must fit
def photo(date_str, meal_id):
    """Owner photo proxy. Auth is inline (NOT approved_required): its redirect
    would send a background <img> from an expired session into /login, where
    it rewrites oauth_state/login_next and can break an in-flight sign-in.
    An image request gets a plain 404 instead — nothing to enumerate."""
    user = auth.current_user()  # fresh row read, like the decorator
    if not user or user.get('status') not in auth.APPROVED_STATUSES:
        return jsonify({'error': 'Not found'}), 404
    return _serve_photo(user['user_id'], date_str, meal_id,
                        lambda: (jsonify({'error': 'Not found'}), 404),
                        'private, max-age=31536000, immutable')


# --- AI estimators -----------------------------------------------------------
# Optional feature on the operator's OpenAI key, for every approved user.
# Cost control is layered: per-IP rate limit (6/min) + per-user daily cap
# (AI_DAILY_LIMIT per UTC day, race-safe conditional counter in db.py).
# The use is consumed BEFORE the OpenAI call; upstream failures refund it.

def _consume_ai_use_or_429(user_id, route):
    """Returns (today_str, None) when a use was consumed, else (None, response)."""
    today = _utc_today_str()
    try:
        allowed = db.try_consume_ai_use(user_id, today, config.AI_DAILY_LIMIT)
    except Exception as e:
        ref = ai.log_failure('cap', {'route': route, 'user': user_id,
                                     'error': type(e).__name__, 'detail': str(e)})
        return None, (jsonify({
            'error': f'AI estimate unavailable right now [ref {ref}]',
            'ref': ref}), 503)
    if not allowed:
        return None, (jsonify({
            'error': f'Daily AI limit reached ({config.AI_DAILY_LIMIT} estimates '
                     'per day) — try again tomorrow.'}), 429)
    return today, None


def _ai_error_response(user_id, today, err):
    """The user-facing half of a logged failure: the ref shown here is the ref
    in the AI_ERROR log line, so a bug report points straight at the record."""
    message, status, refundable, ref = err
    if refundable:
        db.refund_ai_use(user_id, today)  # best-effort
    return jsonify({'error': f'{message} [ref {ref}]', 'ref': ref}), status


@app.route('/api/estimate-fiber', methods=['POST'])
@limiter.limit('6 per minute')
@auth.approved_required
def estimate_fiber():
    """Estimate the user's tracked nutrient for a meal description.
    (URL kept for history; the estimator follows the user's nutrient config.)"""
    if not config.OPENAI_API_KEY:
        return jsonify({'error': 'AI estimation is not enabled on this server'}), 400
    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    if not description:
        return jsonify({'error': 'Description is required'}), 400
    if len(description) > 500:
        return jsonify({'error': 'Description too long (max 500 characters)'}), 400

    user_id = g.user['user_id']
    today, blocked = _consume_ai_use_or_429(user_id, 'estimate-fiber')
    if blocked:
        return blocked
    result, err = ai.estimate_text(description, config.resolve_nutrient(g.user),
                                   log_context={'user': user_id,
                                                'route': 'estimate-fiber'})
    if err:
        return _ai_error_response(user_id, today, err)
    return jsonify(result)


@app.route('/api/estimate-photo', methods=['POST'])
@limiter.limit('6 per minute')
@auth.approved_required
def estimate_photo():
    """Describe a meal photo and estimate its tracked nutrient (vision)."""
    if not config.OPENAI_API_KEY:
        return jsonify({'error': 'AI estimation is not enabled on this server'}), 400
    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify({'error': 'Photo is required'}), 400
    photo_bytes = photo.read()
    if len(photo_bytes) > 8 * 1024 * 1024:
        return jsonify({'error': 'Photo too large for estimation'}), 400
    try:
        photo_bytes = imaging.to_jpeg(photo_bytes)  # HEIC/any -> JPEG for the vision API
    except ValueError as e:
        # Logged like an upstream failure: "couldn't read that image" is a
        # common report, and the decoder's reason is the whole diagnosis.
        ref = ai.log_failure('image', {
            'route': 'estimate-photo', 'user': g.user['user_id'], 'mode': 'photo',
            'error': type(e).__name__, 'detail': str(e),
            'photo_kb': len(photo_bytes) // 1024})
        return jsonify({
            'error': f"Couldn't read that image — try a JPEG or PNG [ref {ref}]",
            'ref': ref}), 400

    user_id = g.user['user_id']
    today, blocked = _consume_ai_use_or_429(user_id, 'estimate-photo')
    if blocked:
        return blocked
    result, err = ai.estimate_photo(photo_bytes, config.resolve_nutrient(g.user),
                                    log_context={'user': user_id,
                                                 'route': 'estimate-photo'})
    if err:
        return _ai_error_response(user_id, today, err)
    return jsonify(result)


# --- Auto-log (batch "auto-add from photos"; async) --------------------------
# The upload only validates and spools the photo on LOCAL DISK (autolog.py);
# the AI estimate and the meal write happen later in the background worker, so
# the client can close the page right after the uploads finish. The queue is
# the spool directory itself — no extra metadata store.

@app.route('/api/auto-log', methods=['POST'])
@limiter.limit('60 per minute')
@auth.approved_required
def auto_log():
    """Queue ONE batch photo (multipart: photo + date + time, both from the
    client — the time normally read from the photo's EXIF)."""
    # Needs BOTH halves: the worker's whole job is estimate + photo meal.
    if not config.OPENAI_API_KEY or not config.S3_BUCKET:
        return jsonify({'error': 'Auto-add is not enabled on this server'}), 400

    # Same client-date discipline as add_meal (invariant #10).
    date_str = request.form.get('date')
    if not date_str:
        return jsonify({'error': 'Date is required'}), 400
    if not _valid_date(date_str):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    if date.fromisoformat(date_str) > datetime.now(timezone.utc).date() + timedelta(days=1):
        return jsonify({'error': "Date can't be in the future"}), 400
    # Unlike add_meal there is no "now" fallback: by the time the worker runs,
    # "now" is wrong — the whole point is logging at the photo's own time.
    time_str = (request.form.get('time') or '').strip()
    try:
        datetime.strptime(time_str, '%H:%M')
    except ValueError:
        return jsonify({'error': 'Invalid time format. Use HH:MM'}), 400

    user_id = g.user['user_id']

    # Time-based dedup: the EXIF minute is a stable fingerprint, so re-running
    # a batch ("did Tuesday's upload include the lunch photo?") skips photos
    # already logged — or already queued — at that minute instead of
    # double-logging the day. Only PHOTO meals block: a typed text-only meal
    # at the same minute doesn't hide new photo information. Deliberately a
    # heuristic: a second photo taken within the same minute is treated as
    # the same meal.
    #
    # The MEAL scan covers a window, not just this photo's date: a duplicate
    # can sit on a nearby day when the date was unreliable the first time (a
    # camera clock ahead of reality gets clamped to "today"; an EXIF-less
    # file's modified-time stamp can drift between selections). ?since= +
    # ?since_time= — the EXIF stamp of the batch's OLDEST photo,
    # client-computed — anchor the window AT that photo (not at its
    # midnight), so a habitual same-minute meal on an unrelated older day —
    # or earlier on the oldest day than the batch even starts — never
    # blocks; the end is UTC tomorrow (nothing is ever logged past it).
    # Bounded at 31 days so a bogus since can't demand an unbounded range
    # Query. The SPOOL check stays exact date+time: pending entries are this
    # batch's own trustworthy dates, and a window there would misfire on two
    # same-minute photos from different days of one batch.
    hhmm = time_str.replace(':', '')
    since = request.form.get('since')
    win_start = date_str
    since_hhmm = None  # sub-day start: set only when the window opens ON the since day
    if since and _valid_date(since) and since < date_str:
        win_start = max(since,
                        (date.fromisoformat(date_str) - timedelta(days=31)).isoformat())
        if win_start == since:
            try:
                datetime.strptime(request.form.get('since_time') or '', '%H:%M')
                since_hhmm = request.form['since_time'].replace(':', '')
            except ValueError:
                pass  # absent/malformed: the whole since day is in the window

    def _blocks(m):
        mid = str(m.get('meal_id', ''))
        if not m.get('photo_key') or not mid.startswith(hhmm):
            return False
        # A meal on the window's first day BEFORE the batch's oldest photo
        # predates this batch — it can't be one of these photos' duplicates.
        if since_hhmm and m.get('date') == win_start and mid[:4] < since_hhmm:
            return False
        return True

    win_end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    try:
        already = any(_blocks(m)
                      for m in db.query_meals_range(user_id, win_start, win_end))
    except Exception as e:
        print(f"Error checking auto-log duplicates for user {user_id}: {type(e).__name__}")
        already = False  # fail open: a duplicate meal beats a lost one
    if already or autolog.has_pending(user_id, date_str, time_str):
        return jsonify({'queued': False, 'skipped': True,
                        'pending': autolog.pending_count(user_id)}), 200

    if autolog.pending_count(user_id) >= config.AUTOLOG_MAX_PENDING:
        return jsonify({'error': f'Too many photos queued (max '
                                 f'{config.AUTOLOG_MAX_PENDING}) — wait for '
                                 'processing to catch up'}), 429

    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify({'error': 'Photo is required'}), 400
    # Normalize NOW so an unreadable image fails while the user is still
    # looking, and the spool holds ready-to-use JPEG bytes.
    try:
        jpeg_bytes = imaging.to_jpeg(photo.read())
    except ValueError:
        return jsonify({'error': "Couldn't read that image — try a JPEG or PNG"}), 400

    try:
        autolog.enqueue(user_id, date_str, time_str, jpeg_bytes)
    except OSError as e:
        print(f"Error spooling auto-log photo for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Upload failed — please try again'}), 500
    autolog.ensure_worker()
    return jsonify({'queued': True,
                    'pending': autolog.pending_count(user_id)}), 202


@app.route('/api/auto-log/pending')
@auth.approved_required
def auto_log_pending():
    """The session user's queue depth (for the page's progress poll)."""
    autolog.ensure_worker()
    return jsonify({'pending': autolog.pending_count(g.user['user_id'])})


# --- Review ------------------------------------------------------------------

@app.route('/review')
@auth.approved_required
def review_page():
    return render_template('review.html', user=g.user,
                           nutrient=config.resolve_nutrient(g.user),
                           meals_url='/api/meals')


# --- Invite links ------------------------------------------------------------
# /i/<token> is public, token-gated, rate-limited. Redemption itself happens
# ONLY server-side in /callback — nothing from a URL can set account status.
# The four dead states (missing/revoked/expired/used) render byte-identical
# 404s: no enumeration oracle, and the token never lands in the page body.

@app.route('/i/<token>')
@limiter.limit('30 per minute')
def invite_view(token):
    try:
        valid = _valid_invite_for_redemption(token)
    except Exception as e:
        print(f"Error resolving invite: {type(e).__name__}")
        valid = None
    if valid is None:
        return render_template('invite_404.html', user=auth.current_user(),
                               login_next='/'), 404
    _, inviter = valid
    # Attribution: inviter NAME only — never the email. login_url routes the
    # corner-menu sign-in through the invite too — without it, that link
    # would silently drop the invite and strand the friend in the queue.
    return render_template('invite_view.html', user=auth.current_user(),
                           inviter_name=inviter.get('name') or 'A Ndiro user',
                           token=token,
                           login_url=f'/login?invite={token}&next=/log')


# --- Share links -------------------------------------------------------------
# /s/<token> is public, token-gated, rate-limited, read-only, and fully
# session-independent: everything is scoped to the token row's user_id.
# Missing, revoked, and expired tokens are INDISTINGUISHABLE (same 404 body).

def _resolve_share(token):
    """Share row if the token is valid AND active, else None (no reason given)."""
    try:
        row = db.get_share(token)
    except Exception as e:
        print(f"Error resolving share token: {type(e).__name__}")
        return None
    return row if db.share_is_active(row) else None


@app.route('/s/<token>')
@limiter.limit('30 per minute')
def share_view(token):
    share = _resolve_share(token)
    if share is None:
        # Byte-identical for missing/revoked/expired — no enumeration oracle.
        # login_next='/' keeps the token path out of the page for that reason.
        return render_template('share_404.html', user=auth.current_user(),
                               login_next='/'), 404
    # Attribution for recipients: name + picture ONLY — never the email.
    # The nutrient config comes from the SAME owner row: the shared data must
    # be labeled with the owner's tracked micro, never the viewer's. A failed
    # read degrades to the fiber default (like the attribution) — no leak.
    owner = None
    nutrient = config.resolve_nutrient(None)
    try:
        row = db.get_user(share['user_id'])
        if row is not None:
            owner = {'name': row.get('name') or '',
                     'picture': row.get('picture') or ''}
            nutrient = config.resolve_nutrient(row)
    except Exception as e:
        print(f"Error reading share owner: {type(e).__name__}")
    # user is chrome-only (corner menu); the meal data is scoped to the token
    # row's user_id and never to the session.
    return render_template('share_view.html',
                           user=auth.current_user(),
                           owner=owner,
                           nutrient=nutrient,
                           meals_url=f'/s/{token}/meals')


def _share_404():
    """The ONE dead-share JSON body: every share data/photo failure mode
    (dead token, missing meal, missing photo) must be byte-identical."""
    return jsonify({'error': 'This link has expired or is no longer available'}), 404


@app.route('/s/<token>/meals')
@limiter.limit('30 per minute')
def share_meals(token):
    share = _resolve_share(token)
    if share is None:
        return _share_404()
    # Month reads only — the minimal surface the share page needs.
    args = {'anchor': request.args.get('anchor')}
    month = request.args.get('month')
    if month:
        args['month'] = month
    else:
        anchor = args['anchor']
        args['month'] = anchor[:7] if anchor and _valid_date(anchor) \
            else datetime.now(timezone.utc).strftime('%Y-%m')
    payload, status = _meals_payload(share['user_id'], args,
                                     photo_base=f'/s/{token}/photo')
    return jsonify(payload), status


@app.route('/s/<token>/photo/<date_str>/<meal_id>')
@limiter.limit('600 per minute')  # per-image; the page/data routes stay at 30
def share_photo(token, date_str, meal_id):
    """Token-scoped photo proxy: owner comes from the token row, the session
    is never consulted. max-age is ONE DAY (not the owner route's year) so a
    revoked recipient's browser cache ages out quickly."""
    share = _resolve_share(token)
    if share is None:
        return _share_404()
    return _serve_photo(share['user_id'], date_str, meal_id, _share_404,
                        'private, max-age=86400')


def _share_to_json(row):
    expires_at = row.get('expires_at')
    return {
        'token': row['share_token'],
        'url': f"/s/{row['share_token']}",
        'label': row.get('label', ''),
        'created_at': row.get('created_at'),
        'expires_at': int(expires_at) if expires_at is not None else None,
        'revoked': bool(row.get('revoked')),
        'active': db.share_is_active(row),
    }


@app.route('/shares')
@auth.approved_required
def shares_page():
    return render_template('shares.html', user=g.user)


@app.route('/api/shares')
@auth.approved_required
def list_shares():
    try:
        rows = db.list_user_shares(g.user['user_id'])
    except Exception as e:
        print(f"Error listing shares: {type(e).__name__}")
        return jsonify({'error': 'Failed to list share links'}), 500
    rows.sort(key=lambda r: r.get('created_at') or '', reverse=True)
    return jsonify({'shares': [_share_to_json(r) for r in rows],
                    'max_active': config.MAX_ACTIVE_SHARES})


@app.route('/api/shares', methods=['POST'])
@auth.approved_required
def create_share():
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or '').strip()
    if len(label) > 100:
        return jsonify({'error': 'Label too long (max 100 characters)'}), 400
    expires = str(data.get('expires') or 'never')
    if expires not in ('7', '30', '90', 'never'):
        return jsonify({'error': 'expires must be 7, 30, 90 or never'}), 400
    expires_at = None if expires == 'never' else int(time.time()) + int(expires) * 86400

    user_id = g.user['user_id']
    try:
        active = [r for r in db.list_user_shares(user_id) if db.share_is_active(r)]
        if len(active) >= config.MAX_ACTIVE_SHARES:
            return jsonify({'error': f'Limit of {config.MAX_ACTIVE_SHARES} active '
                                     'share links reached — revoke one first'}), 400
        row = db.create_share(user_id, label or None, expires_at)
    except Exception as e:
        print(f"Error creating share for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to create share link'}), 500
    return jsonify(_share_to_json(row)), 201


@app.route('/api/shares/<token>', methods=['DELETE'])
@auth.approved_required
def revoke_share(token):
    """Revoke = flip the flag, conditioned on ownership; the row is kept."""
    try:
        ok = db.revoke_share(token, g.user['user_id'])
    except Exception as e:
        print(f"Error revoking share: {type(e).__name__}")
        return jsonify({'error': 'Failed to revoke share link'}), 500
    if not ok:
        return jsonify({'error': 'Share link not found'}), 404
    return jsonify({'revoked': True})


# --- Invite management -------------------------------------------------------

def _invite_to_json(row):
    """Inviter-facing view. Deliberately NO used_by: the invitee's identity is
    not the inviter's to see — the label is the inviter's own note."""
    return {
        'token': row['invite_token'],
        'url': f"/i/{row['invite_token']}",
        'label': row.get('label', ''),
        'created_at': row.get('created_at'),
        'expires_at': int(row['expires_at']) if row.get('expires_at') is not None else None,
        'revoked': bool(row.get('revoked')),
        'used': 'used_by' in row,
        'used_at': row.get('used_at'),
        'active': db.invite_is_active(row),
    }


@app.route('/invites')
@auth.approved_required
def invites_page():
    return render_template('invites.html', user=g.user)


@app.route('/api/invites')
@auth.approved_required
def list_invites():
    try:
        rows = db.list_user_invites(g.user['user_id'])
    except Exception as e:
        print(f"Error listing invites: {type(e).__name__}")
        return jsonify({'error': 'Failed to list invite links'}), 500
    rows.sort(key=lambda r: r.get('created_at') or '', reverse=True)
    return jsonify({'invites': [_invite_to_json(r) for r in rows],
                    'max_active': config.MAX_ACTIVE_INVITES})


@app.route('/api/invites', methods=['POST'])
@limiter.limit('10 per minute')
@auth.approved_required
def create_invite():
    # NOTE the active-cap below is read-then-write: concurrent creates can
    # overshoot MAX_ACTIVE_INVITES by up to the thread count. The rate limit
    # keeps that bounded; a conditional counter is overkill at PoC scale.
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or '').strip()
    if len(label) > 100:
        return jsonify({'error': 'Label too long (max 100 characters)'}), 400
    expires = str(data.get('expires') or '7')
    if expires not in ('1', '7', '30'):
        return jsonify({'error': 'expires must be 1, 7 or 30 (days)'}), 400
    expires_at = int(time.time()) + int(expires) * 86400

    user_id = g.user['user_id']
    try:
        active = [r for r in db.list_user_invites(user_id) if db.invite_is_active(r)]
        if len(active) >= config.MAX_ACTIVE_INVITES:
            return jsonify({'error': f'Limit of {config.MAX_ACTIVE_INVITES} active '
                                     'invites reached — revoke one first'}), 400
        row = db.create_invite(user_id, expires_at, label or None)
    except Exception as e:
        print(f"Error creating invite for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to create invite link'}), 500
    return jsonify(_invite_to_json(row)), 201


@app.route('/api/invites/<token>', methods=['DELETE'])
@auth.approved_required
def revoke_invite(token):
    """Revoke = flip the flag, conditioned on ownership; the row is kept."""
    try:
        ok = db.revoke_invite(token, g.user['user_id'])
    except Exception as e:
        print(f"Error revoking invite: {type(e).__name__}")
        return jsonify({'error': 'Failed to revoke invite link'}), 500
    if not ok:
        return jsonify({'error': 'Invite link not found'}), 404
    return jsonify({'revoked': True})


# --- Settings / account deletion ---------------------------------------------

@app.route('/settings')
@auth.approved_required
def settings_page():
    nutrient = config.resolve_nutrient(g.user)
    return render_template('settings.html', user=g.user, nutrient=nutrient,
                           catalog=config.NUTRIENT_CATALOG,
                           nutrient_in_catalog=bool(config.catalog_entry(nutrient['key'])))


@app.route('/api/settings/nutrient', methods=['POST'])
@auth.approved_required
def set_nutrient():
    """Set the user's tracked micro: a NUTRIENT_CATALOG key plus an optional
    goal override (prefilled from the catalog default in the UI). The closed
    catalog keeps keys safe — they double as form field names, nutrients map
    keys, and AI schema properties. Writes only the session user's row."""
    data = request.get_json(silent=True) or {}
    user_id = g.user['user_id']

    key = data.get('key')
    entry = config.catalog_entry(key)
    legacy = False
    if entry is None:
        # A pre-catalog free-form row may carry a non-catalog key. Its OWNER
        # may keep it (and adjust its goal) so opening settings never
        # force-switches them; nobody can CREATE a non-catalog micro.
        current = config.resolve_nutrient(g.user)
        if key and key == g.user.get('nutrient_key') and not current['is_default']:
            entry, legacy = current, True
        else:
            return jsonify({'error': 'Pick a micro from the list'}), 400

    raw_goal = data.get('goal')
    if raw_goal is None:
        goal = None  # follow the catalog default (0 sentinel below)
    else:
        # '' is NOT an omission: the settings form always sends the field, so
        # empty means cleared/unparseable input — reject rather than silently
        # resetting a personalized goal to the default.
        try:
            goal = Decimal(str(raw_goal))
        except InvalidOperation:
            return jsonify({'error': 'Goal must be a number'}), 400
        if not goal.is_finite() or goal <= 0 or goal > config.NUTRIENT_GOAL_MAX:
            return jsonify({'error': 'Goal must be greater than 0 and sane'}), 400

    # Catalog micros store goal 0 ("follow the catalog default") unless the
    # user actually deviates — so a future change to a catalog default reaches
    # everyone who never personalized. Legacy rows always store explicitly
    # (the row snapshot is their only definition).
    if legacy:
        stored_goal = goal if goal is not None else Decimal(str(entry['goal']))
    elif goal is None or goal == Decimal(str(entry['goal'])):
        stored_goal = Decimal(0)
    else:
        stored_goal = goal

    try:
        db.set_user_nutrient(user_id, entry['key'], entry['label'],
                             entry['unit'], stored_goal, entry['direction'])
    except Exception as e:
        print(f"Error saving nutrient config for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to save — please try again'}), 500
    return jsonify({'nutrient': config.resolve_nutrient({
        'nutrient_key': entry['key'], 'nutrient_label': entry['label'],
        'nutrient_unit': entry['unit'], 'nutrient_goal': stored_goal,
        'nutrient_direction': entry['direction']})})


@app.route('/api/account/delete', methods=['POST'])
@auth.approved_required
def delete_account():
    """Self-service deletion: wipes the user's meals, their whole
    users/{user_id}/ S3 prefix, their share links, and their user row."""
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'delete':
        return jsonify({'error': 'Confirmation required'}), 400
    user_id = g.user['user_id']
    try:
        # Photos first and STRICTLY: if the S3 wipe is incomplete we abort with
        # the account intact, so the user can retry rather than be left with
        # orphaned private photos and no account. The auto-log spool holds
        # photos too (local disk), so it gets the same strict treatment.
        autolog.drop_user(user_id)
        db.delete_user_photos(user_id)
        db.delete_all_meals(user_id)
        db.delete_user_shares(user_id)
        db.delete_user_invites(user_id)
        db.delete_user(user_id)  # row deleted LAST so a partial failure is retryable
    except Exception as e:
        print(f"Error deleting account {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Account deletion failed — please try again'}), 500
    session.clear()
    print(f"Account deleted: user {user_id}")
    return jsonify({'deleted': True})


if __name__ == '__main__':
    # Debug only when explicitly asked: the Werkzeug debugger allows code
    # execution and must never run on anything reachable from the network.
    app.run(debug=os.getenv('FLASK_DEBUG') == '1', port=5000)
