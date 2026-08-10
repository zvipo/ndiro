"""Shared setup for the stub tests: env vars, fakes, and a sign-in helper.

Import this FIRST in every test script — it must configure the environment
and install the fakes before config/db/app are imported.
"""
import os
import sys

# Test env: placeholders only, set before config.py is imported. Real values
# (if any) from a local .env are irrelevant because os.environ wins.
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['AWS_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'test-access-key-id'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'test-secret-access-key'
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

# Hermetic tests: a developer's real .env must never alter test behavior.
# The explicit os.environ values above are the whole test environment.
import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *args, **kwargs: None

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

# A real 8x8 JPEG for photo-upload tests (the app normalizes uploads via
# imaging.to_jpeg, which rejects non-image bytes).
import base64 as _b64  # noqa: E402
TINY_JPEG = _b64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDeooor509s/9k=')
