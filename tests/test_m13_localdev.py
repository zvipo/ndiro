"""M13: local dev mode (NDIRO_LOCAL_DEV=1) — the whole app on local stand-ins.

Covers: the production-safety guards, the derived SECRET_KEY, the feature
switches, the seed data, the fake Google chooser driving the REAL
/login/google -> /callback flow, the AI stub (answers and failure tags), the
mailbox behind native signup/reset, the auto-log worker against local
storage, persistence across a "restart", account deletion, the admin
monitor over local storage, and the reset button.

Run:  python tests/test_m13_localdev.py

Standalone bootstrap rather than testkit: the mode must be on BEFORE config
is imported, and the point is to exercise localdev.install() itself — the
tables, storage, mailer, Google and AI stand-ins it wires in — not testkit's.
"""
import base64
import io
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DATA_DIR = tempfile.mkdtemp(prefix='ndiro-localdev-test-')
# The dev-mode environment, and nothing else: a developer's shell must not
# steer it (a real .env is neutralized below the same way testkit does).
for _var in ('SECRET_KEY', 'RENDER', 'COOKIE_SECURE', 'LOCAL_DEV_AI',
             'LOCAL_DEV_PHOTOS', 'LOCAL_DEV_EMAIL', 'LOCAL_DEV_SEED',
             'OPENAI_API_KEY', 'S3_BUCKET', 'MAIL_FROM', 'APP_BASE_URL',
             'AUTOLOG_DIR', 'ADMIN_EMAILS', 'MAX_USERS', 'AI_DAILY_LIMIT',
             'GOOGLE_CLIENT_ID', 'NATIVE_ID_SECRET'):
    os.environ.pop(_var, None)
os.environ['NDIRO_LOCAL_DEV'] = '1'
os.environ['LOCAL_DATA_DIR'] = DATA_DIR

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *args, **kwargs: None

import config  # noqa: E402
import db  # noqa: E402
import autolog  # noqa: E402
import localdev  # noqa: E402
import app as app_module  # noqa: E402

autolog.WORKER_ENABLED = False      # drive process_once() by hand
app_module.ASYNC_AUTH_WORK = False  # auth side work inline
localdev.MAILBOX.echo = False       # keep the test output readable
app = app_module.app
app.config['TESTING'] = True
limiter = app_module.limiter

TINY_JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDeooor509s/9k=')

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


def form_token(c):
    c.get('/login')
    with c.session_transaction() as sess:
        return sess['form_token']


def google_sign_in(c, code, next_target='/log'):
    """The REAL flow: /login/google -> (fake chooser) -> /callback."""
    resp = c.get(f'/login/google?next={next_target}')
    assert resp.status_code == 302, resp.status_code
    with c.session_transaction() as sess:
        state = sess['oauth_state']
    return c.get(f'/callback?state={state}&code={code}')


TODAY = datetime.now(timezone.utc).date()

# --- 1. Configuration under the mode ------------------------------------------
check('LOCAL_DEV is on', config.LOCAL_DEV is True)
check('session cookie is not Secure (plain-http localhost)',
      config.COOKIE_SECURE is False and app.config['SESSION_COOKIE_SECURE'] is False)
check('photos, AI, email, and Google sign-in all switched on',
      bool(config.S3_BUCKET and config.OPENAI_API_KEY and config.EMAIL_ENABLED
           and config.GOOGLE_CLIENT_ID))
with open(os.path.join(DATA_DIR, 'secret_key')) as f:
    check('SECRET_KEY derived and persisted in the data dir',
          config.SECRET_KEY and f.read().strip() == config.SECRET_KEY)
check('auto-log spool lives under the data dir',
      config.AUTOLOG_DIR == os.path.join(DATA_DIR, 'autolog'))
check('the seeded admin address bootstraps as admin',
      localdev.PERSONAS[0]['email'] in config.ADMIN_EMAILS)
check('APP_BASE_URL is ignored (links use the request host)', config.APP_BASE_URL == '')

# --- 2. Seed data ------------------------------------------------------------
users = db.list_users()
emails = {u['email'] for u in users}
check('seed: every persona and the native account exist',
      {p['email'] for p in localdev.PERSONAS} | {localdev.NATIVE_PERSONA['email']} <= emails)
by_id = {u['user_id']: u for u in users}
check('seed: every account state is represented',
      {by_id[p['sub']]['status'] for p in localdev.PERSONAS}
      == {'admin', 'approved', 'pending', 'rejected'})
native_row = db.find_user_by_email(localdev.NATIVE_PERSONA['email'], provider='native')
check('seed: native account is verified and approved',
      native_row and native_row.get('email_verified') and native_row['status'] == 'approved')
tendai_meals = db.query_meals_range('dev-tendai',
                                    (TODAY - timedelta(days=27)).isoformat(),
                                    TODAY.isoformat())
check('seed: a month of meals for the approved persona', len(tendai_meals) >= 40)
photo_meals = [m for m in tendai_meals if m.get('photo_key')]
check('seed: photo meals have their JPEGs on local disk',
      photo_meals and all(
          os.path.exists(os.path.join(DATA_DIR, 'photos', *m['photo_key'].split('/')))
          for m in photo_meals))
check('seed: meals carry Decimal nutrients',
      all(isinstance(v, Decimal) for m in tendai_meals for v in m['nutrients'].values()))
check('seed: share links (one active, one revoked) and an open invite',
      sorted(bool(r.get('revoked')) for r in db.list_user_shares('dev-tendai')) == [False, True]
      and any(db.invite_is_active(r) for r in db.list_user_invites('dev-tendai')))
check('seed: the second persona tracks protein',
      config.resolve_nutrient(db.get_user('dev-rudo'))['key'] == 'protein_g')
check('seed: tables persisted as JSON documents',
      all(os.path.exists(os.path.join(DATA_DIR, 'tables', f'{n}.json'))
          for n in ('users', 'meals', 'shares', 'invites')))
check('seed: not repeated over existing data',
      localdev.seed() is False and len(db.list_users()) == len(users))

# --- 3. The console, the banner, the status page -------------------------------
anon = app.test_client()
resp = anon.get('/dev')
body = resp.get_data(as_text=True)
check('/dev renders the console with every persona',
      resp.status_code == 200 and all(p['email'] in body for p in localdev.PERSONAS)
      and localdev.NATIVE_PERSONA['password'] in body)
check('every page carries the dev banner',
      all('Local dev mode' in anon.get(path).get_data(as_text=True)
          for path in ('/', '/privacy', '/login', '/status', '/dzidza')))
check('/status reports the mode (a boolean, not a value)',
      'local dev' in anon.get('/status').get_data(as_text=True))
check('/login offers the (fake) Google button',
      '/login/google' in anon.get('/login').get_data(as_text=True))
check('/dev/google without a sign-in in progress explains itself',
      'No sign-in in progress' in anon.get('/dev/google').get_data(as_text=True))

# --- 4. Fake Google chooser -> the REAL callback --------------------------------
limiter.reset()
admin = app.test_client()
resp = admin.get('/login/google?next=/log')
check('/login/google redirects to the fake chooser',
      resp.status_code == 302 and '/dev/google?state=' in resp.headers['Location'])
chooser = admin.get(resp.headers['Location']).get_data(as_text=True)
check('chooser lists the personas as callback links',
      'code=p.admin' in chooser and 'code=p.tendai' in chooser
      and 'Use another account' in chooser)
with admin.session_transaction() as sess:
    state = sess['oauth_state']
resp = admin.get(f'/callback?state={state}&code=p.admin')
check('admin persona signs in through the real callback',
      resp.status_code == 302 and resp.headers['Location'].endswith('/log'))
check('admin persona is an admin', admin.get('/admin').status_code == 200)
with admin.session_transaction() as sess:
    check('post-login session holds only user_id',
          set(sess.keys()) <= {'user_id', '_permanent'})

tendai = app.test_client()
google_sign_in(tendai, 'p.tendai')
payload = tendai.get(f'/api/meals?days=31&anchor={TODAY.isoformat()}').get_json()
api_meals = [m for d in payload['days'] for m in d['meals']]
check('approved persona sees the seeded meals through the API', len(api_meals) >= 40)
with_photo = next(m for m in api_meals if m['photo_url'])
resp = tendai.get(with_photo['photo_url'])
check('photo proxy serves a seeded photo from local disk',
      resp.status_code == 200 and resp.mimetype == 'image/jpeg'
      and resp.data.startswith(b'\xff\xd8'))
check('log and review pages render',
      tendai.get('/log').status_code == 200 and tendai.get('/review').status_code == 200)
share = next(r for r in db.list_user_shares('dev-tendai') if db.share_is_active(r))
check('the seeded share link works for an anonymous viewer',
      anon.get(f"/s/{share['share_token']}").status_code == 200
      and anon.get(f"/s/{share['share_token']}/meals?anchor={TODAY.isoformat()}").status_code == 200)

farai = app.test_client()
resp = google_sign_in(farai, 'p.farai')
check('pending persona lands on the waiting page',
      resp.headers['Location'].endswith('/waiting')
      and farai.get('/log').headers.get('Location', '').endswith('/waiting'))

blessing = app.test_client()
resp = google_sign_in(blessing, 'p.blessing')
with blessing.session_transaction() as sess:
    no_session = 'user_id' not in sess
check('rejected persona gets no session and lands on /',
      resp.headers['Location'].endswith('/') and no_session)

newbie = app.test_client()
resp = google_sign_in(newbie, localdev.encode_custom('newperson@example.com', 'New Person'))
new_row = db.find_user_by_email('newperson@example.com')
check('a custom account signs up pending through the real callback',
      resp.headers['Location'].endswith('/waiting') and new_row
      and new_row['status'] == 'pending' and new_row['user_id'].startswith('dev-'))
resp = admin.post(f"/api/admin/users/{new_row['user_id']}/approve")
check('admin can approve it, and it can then log',
      resp.status_code == 200 and newbie.get('/log').status_code == 200)

other = app.test_client()
resp = other.get('/login/google?next=/log')
with other.session_transaction() as sess:
    state = sess['oauth_state']
resp = other.post('/dev/google', data={'state': state, 'email': 'Another@Example.com',
                                       'name': 'An Other'})
check('"use another account" redirects into /callback with a custom code',
      resp.status_code == 302 and '/callback?state=' in resp.headers['Location']
      and 'code=c.' in resp.headers['Location'])
resp = other.get(resp.headers['Location'])
check('...and that callback creates the (lower-cased) account',
      resp.status_code == 302 and db.find_user_by_email('another@example.com') is not None)
resp = other.post('/dev/google', data={'state': 'x', 'email': 'nope'})
check('chooser rejects a malformed email',
      resp.status_code == 200 and 'valid email' in resp.get_data(as_text=True))
junk = app.test_client()
resp = google_sign_in(junk, 'not-a-code')
check('an unknown sign-in code is a 400, not an account', resp.status_code == 400)
resp = app.test_client().get('/callback?state=forged&code=p.admin')
check('the real state check still guards the callback', resp.status_code == 400)

quick = app.test_client()
resp = quick.get('/dev/login/tendai?next=/review')
check('console one-click sign-in redirects into the real callback',
      resp.status_code == 302 and '/callback?state=' in resp.headers['Location']
      and 'code=p.tendai' in resp.headers['Location'])
resp = quick.get(resp.headers['Location'])
check('...which signs in and honors next=',
      resp.status_code == 302 and resp.headers['Location'].endswith('/review')
      and quick.get('/review').status_code == 200)
with quick.session_transaction() as sess:
    check('...leaving only user_id in the session', set(sess.keys()) <= {'user_id', '_permanent'})
check('one-click sign-in rejects an unknown persona and an unsafe next',
      app.test_client().get('/dev/login/nobody').status_code == 404
      and '/callback' in app.test_client().get('/dev/login/admin?next=//evil').headers.get('Location', ''))
check('the console offers one-click sign-in per persona',
      all(f'/dev/login/{p["key"]}' in anon.get('/dev').get_data(as_text=True) for p in localdev.PERSONAS))

# --- 5. The AI stub --------------------------------------------------------------
limiter.reset()
resp = tendai.post('/api/estimate-fiber',
                   json={'description': 'Lentil dal with brown rice and a pear'})
est = resp.get_json()
check('stub recognizes guide foods and sums their guide values',
      resp.status_code == 200 and est['model'] == 'local-stub'
      and any(i['food'].startswith('Lentils') for i in est['items'])
      and est['amount'] == round(3.5 + 0.5 + 2.0, 1))
resp = tendai.post('/api/estimate-fiber', json={'description': 'grilled cheese'})
check('unknown foods still get an itemized answer with a note',
      resp.status_code == 200 and resp.get_json()['items']
      and 'Stub' in resp.get_json()['note'])
used_before = int(db.get_user('dev-tendai').get('ai_uses_today') or 0)
resp = tendai.post('/api/estimate-fiber', json={'description': 'toast [fail]'})
check('[fail] tag: 502 with a ref, and the AI use is refunded',
      resp.status_code == 502 and resp.get_json().get('ref')
      and int(db.get_user('dev-tendai').get('ai_uses_today') or 0) == used_before)
resp = tendai.post('/api/estimate-fiber', json={'description': 'toast [garbage]'})
check('[garbage] tag: 502, and the use is NOT refunded',
      resp.status_code == 502
      and int(db.get_user('dev-tendai').get('ai_uses_today') or 0) == used_before + 1)
resp = tendai.post('/api/estimate-photo',
                   data={'photo': (io.BytesIO(TINY_JPEG), 'p.jpg')},
                   content_type='multipart/form-data')
est = resp.get_json()
check('photo stub returns a stock description and items',
      resp.status_code == 200 and est['description'] in
      {d for d, _ in localdev.STOCK_PHOTO_MEALS} and est['items'] and est['amount'] > 0)
rudo = app.test_client()
google_sign_in(rudo, 'p.rudo')
resp = rudo.post('/api/estimate-fiber', json={'description': 'Chicken and rice'})
est = resp.get_json()
check('a custom micro gets made-up amounts labeled as such',
      resp.status_code == 200 and est['amount'] > 0 and 'protein' in est['note'])

# --- 6. The mailbox behind native signup / reset ---------------------------------
limiter.reset()
signup = app.test_client()
resp = signup.post('/signup', data={
    'form_token': form_token(signup), 'email': 'signup@example.com',
    'password': 'correct horse battery', 'name': 'Sign Up', 'next': '/log'})
messages = localdev.MAILBOX.messages
check('signup lands the verification mail in the mailbox',
      resp.status_code == 200 and messages
      and messages[-1]['to'] == 'signup@example.com'
      and any('/verify-email/' in link for link in messages[-1]['links']))
link = next(link for link in messages[-1]['links'] if '/verify-email/' in link)
check('mail links use the request host, not a configured base',
      link.startswith('http://localhost/'))
check('/dev shows the link ready to click',
      link in signup.get('/dev').get_data(as_text=True))
path = link[len('http://localhost'):]
check('the emailed link is a live verify page',
      signup.get(path).status_code == 200)
resp = signup.post(path)
check('verifying through it works', resp.status_code == 302
      and resp.headers['Location'].endswith('/login?verified=1'))
resp = signup.post('/login/password', data={
    'form_token': form_token(signup), 'email': 'signup@example.com',
    'password': 'correct horse battery', 'next': '/log'})
check('the new native account can sign in (pending -> waiting page)',
      resp.status_code == 302 and resp.headers['Location'].endswith('/waiting'))

nyasha = app.test_client()
resp = nyasha.post('/login/password', data={
    'form_token': form_token(nyasha), 'email': localdev.NATIVE_PERSONA['email'],
    'password': localdev.NATIVE_PERSONA['password'], 'next': '/log'})
check('the seeded native persona signs in with the shown password',
      resp.status_code == 302 and resp.headers['Location'].endswith('/log')
      and nyasha.get('/log').status_code == 200)
resp = nyasha.post('/forgot', data={'form_token': form_token(nyasha),
                                    'email': localdev.NATIVE_PERSONA['email']})
check('forgot-password mail lands in the mailbox with a reset link',
      resp.status_code == 200 and messages[-1]['subject'].startswith('Reset')
      and any('/reset/' in link for link in messages[-1]['links']))
nyasha.get('/dev')
with nyasha.session_transaction() as sess:
    token = sess['form_token']
check('clearing the mailbox needs the form token',
      nyasha.post('/dev/mail/clear', data={}).status_code == 400)
resp = nyasha.post('/dev/mail/clear', data={'form_token': token})
check('clear mailbox empties it', resp.status_code == 302 and not localdev.MAILBOX.messages)

# --- 7. Auto-log worker against local storage ------------------------------------
limiter.reset()
resp = tendai.post('/api/auto-log', data={
    'photo': (io.BytesIO(TINY_JPEG), 'p.jpg'), 'date': TODAY.isoformat(), 'time': '09:41',
}, content_type='multipart/form-data')
check('auto-log accepts a batch photo into the local spool',
      resp.status_code == 202 and os.listdir(config.AUTOLOG_DIR))
done = autolog.process_once()
committed = [m for m in db.query_meals_day('dev-tendai', TODAY.isoformat())
             if m['meal_id'].startswith('0941')]
check('the worker commits it as a photo meal with a stub description',
      done == 1 and committed and committed[0].get('photo_key')
      and committed[0].get('ai_assisted')
      and committed[0]['description'] in {d for d, _ in localdev.STOCK_PHOTO_MEALS})
check('...with the photo stored under the data dir',
      os.path.exists(os.path.join(DATA_DIR, 'photos', *committed[0]['photo_key'].split('/'))))

# --- 8. Persistence and the storage guards ----------------------------------------
reopened = localdev.make_tables(os.path.join(DATA_DIR, 'tables'))
sample = next(iter(reopened.meals.items.values()))
check('tables reload from disk with the same rows and Decimal intact',
      len(reopened.meals.items) == len(localdev.TABLES.meals.items)
      and len(reopened.users.items) == len(localdev.TABLES.users.items)
      and all(isinstance(v, Decimal) for v in sample['nutrients'].values())
      and isinstance(reopened.users.items[('dev-rudo',)]['nutrient_goal'], Decimal))
reopened_s3 = localdev.LocalS3(os.path.join(DATA_DIR, 'photos'))
check('photos reload from disk', reopened_s3.keys() == localdev.S3.keys()
      and len(reopened_s3.keys()) > 0)
try:
    localdev.S3.put_object(Bucket='x', Key='../escape.jpg', Body=b'x')
    refused = False
except ValueError:
    refused = True
check('LocalS3 refuses a traversal key on write',
      refused and not os.path.exists(os.path.join(DATA_DIR, 'escape.jpg')))
try:
    localdev.S3.get_object(Bucket='x', Key='../../etc/passwd')
    refused = False
except Exception as e:
    refused = type(e).__name__ == 'ClientError'
check('LocalS3 answers NoSuchKey for a traversal key on read', refused)

# --- 9. Deletion and the monitor over local storage --------------------------------
before_photos = len(localdev.S3.keys())
resp = tendai.post('/api/account/delete', json={'confirm': 'delete'})
check('account deletion wipes meals, photos, links, and the row',
      resp.status_code == 200 and db.get_user('dev-tendai') is None
      and not db.query_meals_range('dev-tendai', '2000-01-01', '2999-12-31')
      and not [k for k in localdev.S3.keys() if k.startswith('users/dev-tendai/')]
      and not db.list_user_shares('dev-tendai') and len(localdev.S3.keys()) < before_photos)
stats = admin.get(f'/api/admin/stats?anchor={TODAY.isoformat()}').get_json()
check('admin monitor counts local photos and accounts',
      stats['photos']['enabled'] and stats['photos']['total'] == len(localdev.S3.keys())
      and stats['accounts']['total'] == len(db.list_users())
      and stats['orphans'] == {'meals': 0, 'photos': 0})

# --- 10. Reset to seed data ---------------------------------------------------------
admin.get('/dev')
with admin.session_transaction() as sess:
    token = sess['form_token']
check('reset needs the form token', admin.post('/dev/reset', data={}).status_code == 400)
resp = admin.post('/dev/reset', data={'form_token': token})
seed_count = len(localdev.PERSONAS) + 1
check('reset wipes the extras and reloads the seed',
      resp.status_code == 302 and len(db.list_users()) == seed_count
      and db.get_user('dev-tendai') is not None
      and len(db.query_meals_range('dev-tendai', '2000-01-01', '2999-12-31')) >= 40
      and not localdev.MAILBOX.messages and not os.listdir(config.AUTOLOG_DIR))
check('the admin session survives a reset (same seeded ids)',
      admin.get('/admin').status_code == 200)

# --- 11. Boot-time guards (fresh interpreters) --------------------------------------
BASE_ENV = {k: v for k, v in os.environ.items()
            if k not in ('NDIRO_LOCAL_DEV', 'LOCAL_DATA_DIR')}


def boot(extra, code='import config'):
    env = {**BASE_ENV, 'AUTOLOG_DIR': tempfile.mkdtemp(prefix='ndiro-boot-'), **extra}
    return subprocess.run(
        [sys.executable, '-c',
         'import dotenv; dotenv.load_dotenv = lambda *a, **k: None; ' + code],
        capture_output=True, text=True, cwd=REPO, env=env)


r = boot({'NDIRO_LOCAL_DEV': '1', 'RENDER': 'true'})
check('refuses to run on Render', r.returncode != 0 and 'RuntimeError' in r.stderr)
r = boot({'NDIRO_LOCAL_DEV': '1', 'COOKIE_SECURE': '1'})
check('refuses an explicit COOKIE_SECURE=1', r.returncode != 0 and 'RuntimeError' in r.stderr)
r = boot({'NDIRO_LOCAL_DEV': '1', 'LOCAL_DEV_AI': 'bogus'})
check('rejects an unknown LOCAL_DEV_AI', r.returncode != 0 and 'RuntimeError' in r.stderr)
r = boot({'NDIRO_LOCAL_DEV': '1', 'LOCAL_DEV_AI': 'real'})
check('LOCAL_DEV_AI=real without a key is refused',
      r.returncode != 0 and 'RuntimeError' in r.stderr)
r = boot({'NDIRO_LOCAL_DEV': '0'})
check('with the mode off, a missing SECRET_KEY still hard-fails',
      r.returncode != 0 and 'RuntimeError' in r.stderr)
r = boot({'NDIRO_LOCAL_DEV': '1', 'LOCAL_DEV_AI': 'off', 'LOCAL_DEV_PHOTOS': '0',
          'LOCAL_DEV_EMAIL': '0'},
         'import config; assert config.SECRET_KEY; assert config.OPENAI_API_KEY is None; '
         'assert config.S3_BUCKET is None; assert not config.EMAIL_ENABLED; '
         'assert config.LOCAL_DATA_DIR is None')
check('feature switches turn AI, photos, and email off; no data dir = in memory',
      r.returncode == 0, )
r = boot({'NDIRO_LOCAL_DEV': '1', 'LOCAL_DEV_SEED': '0'},
         'import autolog; autolog.WORKER_ENABLED = False; import app, db; '
         'assert db.list_users() == []; '
         'assert app.app.test_client().get("/dev").status_code == 200')
check('in-memory mode boots without a data dir; LOCAL_DEV_SEED=0 starts empty',
      r.returncode == 0, )
if r.returncode:
    print(r.stderr[-2000:])

finish('M13 local dev mode')
