"""Shared setup for the stub tests: env vars, fakes, and a sign-in helper.

Import this FIRST in every test script — it must configure the environment
and install the fakes before config/db/app are imported.
"""
import os
import sys

# Test env: placeholders only, set before config.py is imported. Real values
# (if any) from a local .env are irrelevant because os.environ wins.
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['GOOGLE_CLIENT_ID'] = 'test-client-id.apps.googleusercontent.com'
os.environ['GOOGLE_CLIENT_SECRET'] = 'test-client-secret'
os.environ['GOOGLE_REDIRECT_URI'] = 'https://ndiro.test/callback'
os.environ['ADMIN_EMAILS'] = 'admin@example.test'
os.environ['MAX_USERS'] = '100'
os.environ['S3_BUCKET'] = 'fake-test-bucket'
os.environ['USERS_TABLE'] = 'test-users'
os.environ['MEALS_TABLE'] = 'test-meals'
os.environ['SHARES_TABLE'] = 'test-shares'
os.environ.pop('OPENAI_API_KEY', None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import fakes  # noqa: E402

FIXTURES = fakes.install(db)

import auth  # noqa: E402
import app as app_module  # noqa: E402

app = app_module.app
app.config['TESTING'] = True
limiter = app_module.limiter  # tests may .reset() between sections

BASE_URL = 'https://ndiro.test'  # https so the Secure session cookie flows


def client():
    return app.test_client()


def get(c, url, **kwargs):
    return c.get(url, base_url=BASE_URL, **kwargs)


def post(c, url, **kwargs):
    return c.post(url, base_url=BASE_URL, **kwargs)


def put(c, url, **kwargs):
    return c.put(url, base_url=BASE_URL, **kwargs)


def delete(c, url, **kwargs):
    return c.delete(url, base_url=BASE_URL, **kwargs)


def session(c):
    """Session transaction bound to the https test domain."""
    return c.session_transaction(base_url=BASE_URL)


def sign_in(c, sub, email, name='Test User'):
    """Drive the real /login -> /callback flow with a stubbed Google exchange."""
    auth.fetch_userinfo = lambda code: ({'sub': sub, 'email': email, 'name': name}, None)
    resp = get(c, '/login?next=/log')
    assert resp.status_code == 302, f'/login gave {resp.status_code}'
    with session(c) as sess:
        state = sess['oauth_state']
    return get(c, f'/callback?state={state}&code=stub-code')


_checks = []


def check(label, ok):
    _checks.append((label, bool(ok)))
    print(('  PASS  ' if ok else '  FAIL  ') + label)


def finish(name):
    failed = [label for label, ok in _checks if not ok]
    print()
    if failed:
        print(f'{name}: {len(failed)}/{len(_checks)} checks FAILED')
        sys.exit(1)
    print(f'{name}: all {len(_checks)} checks passed')
    sys.exit(0)
