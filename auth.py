"""Auth for Ndiro: Google OAuth helpers, session -> user resolution, guards.

Session stores ONLY user_id (the Google sub) plus transient oauth_state /
login_next. Status is NEVER cached in the cookie: every guarded request does a
fresh users-table read (~$0.25/million) so a rejected user's live session dies
immediately.
"""
from functools import wraps

import requests
from flask import g, jsonify, redirect, request, session, url_for

import config
import db

GOOGLE_AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_ENDPOINT = 'https://www.googleapis.com/oauth2/v2/userinfo'

APPROVED_STATUSES = ('approved', 'admin')


def _safe_next(target, default='/'):
    """Only allow same-site relative paths as post-login/logout destinations.

    Must start with '/' but not '//' (protocol-relative) and contain no
    backslashes, else fall back to the default. Prevents open redirects.
    """
    if target and target.startswith('/') and not target.startswith('//') and '\\' not in target:
        return target
    return default


def build_auth_url(state):
    """Google authorization URL. Scopes: openid email profile (sub+email+name)."""
    return (
        f'{GOOGLE_AUTH_ENDPOINT}?response_type=code'
        f'&client_id={config.GOOGLE_CLIENT_ID}'
        f'&redirect_uri={config.GOOGLE_REDIRECT_URI}'
        '&scope=openid%20email%20profile'
        '&prompt=select_account'
        f'&state={state}'
    )


def fetch_userinfo(code):
    """Exchange the authorization code and fetch userinfo, server-to-server
    over TLS (which is why the id_token signature is not separately verified).

    Returns ({'sub','email','name','picture'}, None) or (None, error_message).
    """
    token_resp = requests.post(GOOGLE_TOKEN_ENDPOINT, data={
        'client_id': config.GOOGLE_CLIENT_ID,
        'client_secret': config.GOOGLE_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': config.GOOGLE_REDIRECT_URI,
    }, timeout=10)
    if token_resp.status_code != 200:
        return None, 'Failed to exchange authorization code'

    access_token = token_resp.json().get('access_token')
    if not access_token:
        return None, 'No access token returned'

    userinfo_resp = requests.get(
        GOOGLE_USERINFO_ENDPOINT,
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=10,
    )
    if userinfo_resp.status_code != 200:
        return None, 'Failed to fetch user info'

    info = userinfo_resp.json()
    sub = info.get('id') or info.get('sub')  # v2 endpoint calls it 'id'
    email = (info.get('email') or '').lower()
    if not sub or not email:
        return None, 'Google account details incomplete'
    return {'sub': str(sub), 'email': email,
            'name': info.get('name') or '',
            'picture': info.get('picture') or ''}, None


def current_user():
    """The signed-in user's row via a FRESH table read, or None."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    try:
        return db.get_user(user_id)
    except Exception as e:
        print(f"Error reading user row: {type(e).__name__}")
        return None


def _wants_json():
    return request.path.startswith('/api/')


def _deny_unauthenticated():
    session.clear()
    if _wants_json():
        return jsonify({'error': 'Authentication required'}), 401
    return redirect(url_for('login', next=request.full_path if request.query_string else request.path))


def approved_required(f):
    """Guard: only approved (or admin) users. Fresh status read per request:
    pending -> /waiting; rejected/missing -> session cleared -> /."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return _deny_unauthenticated()
        status = user.get('status')
        if status in APPROVED_STATUSES:
            g.user = user
            return f(*args, **kwargs)
        if status == 'pending':
            if _wants_json():
                return jsonify({'error': 'Account awaiting approval'}), 403
            return redirect('/waiting')
        # rejected or unknown: kill the session on the spot
        session.clear()
        if _wants_json():
            return jsonify({'error': 'Account not authorized'}), 401
        return redirect('/')
    return wrapper


def admin_required(f):
    """Guard: admins only, via the same fresh table read (ADMIN_EMAILS only
    bootstraps status at first sign-in — it is not consulted here)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return _deny_unauthenticated()
        if user.get('status') != 'admin':
            if _wants_json():
                return jsonify({'error': 'Admin access required'}), 403
            return redirect('/')
        g.user = user
        return f(*args, **kwargs)
    return wrapper
