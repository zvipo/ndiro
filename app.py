"""Ndiro — a multi-user meal log for viscous soluble fiber tracking.

Flask app + all routes (the table of contents). Support modules:
config.py (env + constants), db.py (DynamoDB/S3), auth.py (OAuth + guards),
ai.py (fiber estimators). All routes live at root so the Google redirect URI
and root-absolute template paths never change.
"""
import calendar
import re
import secrets
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
import config
import imaging
import db

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_MONTH_RE = re.compile(r'^\d{4}-\d{2}$')
_NUTRIENT_MAX = Decimal('100000')  # grams; anything larger is nonsense / a DynamoDB overflow

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
    return jsonify({'status': 'healthy',
                    'timestamp': datetime.now(timezone.utc).isoformat()}), 200


# --- OAuth flow --------------------------------------------------------------

@app.route('/login')
@limiter.limit('10 per minute')
def login():
    if not config.GOOGLE_CLIENT_ID:
        return 'Google login is not configured (set GOOGLE_CLIENT_ID).', 503
    # Remember where to land after the OAuth round-trip. Kept in the session
    # (not the state param) so state stays a pure CSRF token.
    session['login_next'] = auth._safe_next(request.args.get('next'), default='/log')
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    return redirect(auth.build_auth_url(state))


@app.route('/callback')
@limiter.limit('10 per minute')
def callback():
    # CSRF protection: the state must match what we issued in /login.
    if not request.args.get('state') or \
            request.args.get('state') != session.pop('oauth_state', None):
        return 'Invalid OAuth state', 400

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
            # First sign-in. MAX_USERS is enforced HERE, server-side.
            if db.count_users() >= config.MAX_USERS:
                return render_template('full.html'), 403
            status = 'admin' if email in config.ADMIN_EMAILS else 'pending'
            user = db.create_user(user_id, email, name, status, picture)
        elif (user.get('email') != email or user.get('name') != name
              or user.get('picture', '') != picture):
            # sub is the stable key; keep email/name/picture current.
            db.update_user_profile(user_id, email, name, picture)
            user = {**user, 'email': email, 'name': name, 'picture': picture}
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


# --- Admin API (account metadata ONLY — never another user's meals/photos) ---

def _user_to_json(u):
    return {
        'user_id': u.get('user_id'),
        'email': u.get('email'),
        'name': u.get('name'),
        'status': u.get('status'),
        'created_at': u.get('created_at'),
        'approved_at': u.get('approved_at'),
        'ai_uses_date': u.get('ai_uses_date'),
        'ai_uses_today': int(u.get('ai_uses_today', 0)),
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
    return jsonify({'users': [_user_to_json(u) for u in users],
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


# --- Meal log ----------------------------------------------------------------
# Tenant discipline: every handler keys on g.user['user_id'] from the session.
# user_id NEVER comes from the URL, query string, or form.

@app.route('/log')
@auth.approved_required
def log_page():
    return render_template('log.html', user=g.user,
                           fiber_guide=config.FIBER_GUIDE,
                           ai_enabled=bool(config.OPENAI_API_KEY))


def _valid_date(date_str):
    """Return the string if it is a strictly canonical YYYY-MM-DD date, else None.

    strptime alone is lenient ('2026-8-5' parses), which would produce a sort
    key that zero-padded month queries never match (silent data loss) and 500
    when fed to date.fromisoformat as an anchor. Require the round trip.
    """
    if not isinstance(date_str, str) or not _DATE_RE.match(date_str):
        return None
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        return date_str
    except ValueError:
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


def _nutrients_from_form(form):
    """Parse nutrient form fields into a DynamoDB-ready map of Decimals.

    Extensible: add new keys here (e.g. 'protein_g') and the totals/JSON
    plumbing picks them up automatically. Empty string means "unset".
    """
    nutrients = {}
    for field in ('fiber_g',):
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


def _read_meal_form(form):
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
    return description, context, _nutrients_from_form(form)


def _assemble_meal_item(user_id, date_str, meal_id, description, context,
                        nutrients, photo_key, created_at, form):
    """Build a meal item dict (shared by add/edit)."""
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
    return item


def _meal_to_json(item, owner_user_id):
    """DynamoDB meal item -> JSON-safe dict (Decimals become floats).
    Photo URLs are re-signed fresh on every response, only ever under the
    resolved owner's users/{user_id}/ prefix."""
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
        'photo_url': db.presign_photo(photo_key, owner_user_id),
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


def _meals_payload(user_id, args):
    """Build the meals payload for one user: a single date (?date=), a month
    (?month=YYYY-MM), or the last N days (?days=N, default 7, max 31).

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
        meals = [_meal_to_json(item, user_id) for item in by_date[d]]
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
    context/time/fiber_g/photo/ai_assisted)."""
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

    try:
        description, context, nutrients = _read_meal_form(request.form)
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
                               nutrients, photo_key, None, request.form)

    try:
        db.put_meal(item)
    except Exception as e:
        print(f"Error saving meal for user {user_id}: {type(e).__name__}")
        if photo_key:
            db.delete_photo(photo_key)  # don't orphan the just-uploaded object
        return jsonify({'error': 'Failed to save meal'}), 500

    return jsonify(_meal_to_json(item, user_id)), 201


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

    try:
        description, context, nutrients = _read_meal_form(request.form)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

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
                               request.form)

    try:
        db.put_meal(item)
    except Exception as e:
        print(f"Error updating meal for user {user_id}: {type(e).__name__}")
        return jsonify({'error': 'Failed to update meal'}), 500

    if delete_after_commit:
        db.delete_photo(delete_after_commit)  # best-effort, post-commit
    return jsonify(_meal_to_json(item, user_id))


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


# --- AI estimators -----------------------------------------------------------
# Optional feature on the operator's OpenAI key, for every approved user.
# Cost control is layered: per-IP rate limit (6/min) + per-user daily cap
# (AI_DAILY_LIMIT per UTC day, race-safe conditional counter in db.py).
# The use is consumed BEFORE the OpenAI call; upstream failures refund it.

def _consume_ai_use_or_429(user_id):
    """Returns (today_str, None) when a use was consumed, else (None, response)."""
    today = _utc_today_str()
    try:
        allowed = db.try_consume_ai_use(user_id, today, config.AI_DAILY_LIMIT)
    except Exception as e:
        print(f"AI-cap check failed for user {user_id}: {type(e).__name__}")
        return None, (jsonify({'error': 'AI estimate unavailable right now'}), 503)
    if not allowed:
        return None, (jsonify({
            'error': f'Daily AI limit reached ({config.AI_DAILY_LIMIT} estimates '
                     'per day) — try again tomorrow.'}), 429)
    return today, None


def _ai_error_response(user_id, today, err):
    message, status, refundable = err
    if refundable:
        db.refund_ai_use(user_id, today)  # best-effort
    return jsonify({'error': message}), status


@app.route('/api/estimate-fiber', methods=['POST'])
@limiter.limit('6 per minute')
@auth.approved_required
def estimate_fiber():
    """Estimate viscous fiber for a meal description."""
    if not config.OPENAI_API_KEY:
        return jsonify({'error': 'AI estimation is not enabled on this server'}), 400
    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    if not description:
        return jsonify({'error': 'Description is required'}), 400
    if len(description) > 500:
        return jsonify({'error': 'Description too long (max 500 characters)'}), 400

    user_id = g.user['user_id']
    today, blocked = _consume_ai_use_or_429(user_id)
    if blocked:
        return blocked
    result, err = ai.estimate_text(description)
    if err:
        return _ai_error_response(user_id, today, err)
    return jsonify(result)


@app.route('/api/estimate-photo', methods=['POST'])
@limiter.limit('6 per minute')
@auth.approved_required
def estimate_photo():
    """Describe a meal photo and estimate its viscous fiber (vision)."""
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
    except ValueError:
        return jsonify({'error': "Couldn't read that image — try a JPEG or PNG"}), 400

    user_id = g.user['user_id']
    today, blocked = _consume_ai_use_or_429(user_id)
    if blocked:
        return blocked
    result, err = ai.estimate_photo(photo_bytes)
    if err:
        return _ai_error_response(user_id, today, err)
    return jsonify(result)


# --- Review ------------------------------------------------------------------

@app.route('/review')
@auth.approved_required
def review_page():
    return render_template('review.html', user=g.user,
                           goal_g=config.VISCOUS_FIBER_GOAL_G,
                           meals_url='/api/meals')


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
    owner = None
    try:
        row = db.get_user(share['user_id'])
        if row is not None:
            owner = {'name': row.get('name') or '',
                     'picture': row.get('picture') or ''}
    except Exception as e:
        print(f"Error reading share owner: {type(e).__name__}")
    # user is chrome-only (corner menu); the meal data is scoped to the token
    # row's user_id and never to the session.
    return render_template('share_view.html',
                           user=auth.current_user(),
                           owner=owner,
                           goal_g=config.VISCOUS_FIBER_GOAL_G,
                           meals_url=f'/s/{token}/meals')


@app.route('/s/<token>/meals')
@limiter.limit('30 per minute')
def share_meals(token):
    share = _resolve_share(token)
    if share is None:
        return jsonify({'error': 'This link has expired or is no longer available'}), 404
    # Month reads only — the minimal surface the share page needs.
    args = {'anchor': request.args.get('anchor')}
    month = request.args.get('month')
    if month:
        args['month'] = month
    else:
        anchor = args['anchor']
        args['month'] = anchor[:7] if anchor and _valid_date(anchor) \
            else datetime.now(timezone.utc).strftime('%Y-%m')
    payload, status = _meals_payload(share['user_id'], args)
    return jsonify(payload), status


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


# --- Settings / account deletion ---------------------------------------------

@app.route('/settings')
@auth.approved_required
def settings_page():
    return render_template('settings.html', user=g.user)


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
        # orphaned private photos and no account.
        db.delete_user_photos(user_id)
        db.delete_all_meals(user_id)
        db.delete_user_shares(user_id)
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
