"""Ndiro — a multi-user meal log for viscous soluble fiber tracking.

Flask app + all routes (the table of contents). Support modules:
config.py (env + constants), db.py (DynamoDB/S3), auth.py (OAuth + guards),
ai.py (fiber estimators). All routes live at root so the Google redirect URI
and root-absolute template paths never change.
"""
import secrets
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify, redirect, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import config
import db

app = Flask(__name__)

# Behind one reverse proxy (Caddy / Render). Trust X-Forwarded-Proto/Host so
# Flask builds https URLs and marks the session cookie Secure correctly.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.secret_key = config.SECRET_KEY  # config hard-fails at import when unset
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
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
    try:
        user = db.get_user(user_id)
        if user is None:
            # First sign-in. MAX_USERS is enforced HERE, server-side.
            if db.count_users() >= config.MAX_USERS:
                return render_template('full.html'), 403
            status = 'admin' if email in config.ADMIN_EMAILS else 'pending'
            user = db.create_user(user_id, email, name, status)
        elif user.get('email') != email or user.get('name') != name:
            # sub is the stable key; keep email/name current.
            db.update_user_profile(user_id, email, name)
            user = {**user, 'email': email, 'name': name}
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


if __name__ == '__main__':
    app.run(debug=True, port=5000)
