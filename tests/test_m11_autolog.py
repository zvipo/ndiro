"""M11 exit test (stubbed AWS + Google + OpenAI): the async auto-log spool —
endpoint gating/validation, disk queue mechanics, background processing
(estimate accepted, EXIF date/time honored), AI cap + refund semantics,
estimate caching across commit retries, dead-lettering, tenant scoping of the
pending count, rejected-user discard, and account-deletion purge.

Run:  python tests/test_m11_autolog.py
"""
import io
import json
import os

import testkit as tk

import ai
import autolog
import config
import db

DAY = '2026-08-29'


def upload(c, time_str='13:37', date_str=DAY, photo=None):
    return tk.post(c, '/api/auto-log', data={
        'date': date_str, 'time': time_str,
        'photo': (io.BytesIO(photo if photo is not None else tk.TINY_JPEG), 'p.jpg'),
    }, content_type='multipart/form-data')


def spool_files():
    return sorted(os.listdir(config.AUTOLOG_DIR))


def meals_on(c, day=DAY):
    data = tk.get(c, f'/api/meals?date={day}&anchor={day}').get_json()
    return data['days'][0]['meals']


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.headers = {}

    def json(self):
        return self._body


def stub_openai(content=None, exc=None, calls=None):
    def fake_post(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append(url)
        if exc:
            raise exc
        return FakeResp(200, {'choices': [{'message': {'content': content}}]})
    ai.requests.post = fake_post


GOOD = json.dumps({
    'viscous_fiber_g': 4.5,
    'description': 'Dal with brown rice',
    'items': [{'food': 'Lentils (dal), cooked', 'serving': '1 cup', 'grams': 3.5}],
    'note': '',
})

# --- Setup --------------------------------------------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
tk.post(admin, '/api/admin/users/sub-alice/approve')
UID = 'sub-alice'

# --- Gating -------------------------------------------------------------------
config.OPENAI_API_KEY = None
tk.check('no AI key: auto-log 400', upload(alice).status_code == 400)
config.OPENAI_API_KEY = 'test-key-not-real'
_bucket = config.S3_BUCKET
config.S3_BUCKET = None
tk.check('no bucket: auto-log 400', upload(alice).status_code == 400)
config.S3_BUCKET = _bucket
tk.check('anonymous: auto-log denied',
         tk.post(tk.client(), '/api/auto-log', data={}).status_code in (302, 401))
tk.check('anonymous: pending denied',
         tk.get(tk.client(), '/api/auto-log/pending').status_code in (302, 401))

# --- Validation ---------------------------------------------------------------
tk.check('missing date 400', tk.post(alice, '/api/auto-log', data={
    'time': '12:00', 'photo': (io.BytesIO(tk.TINY_JPEG), 'p.jpg')},
    content_type='multipart/form-data').status_code == 400)
tk.check('bad date 400', upload(alice, date_str='2026-8-5').status_code == 400)
tk.check('future date 400', upload(alice, date_str='2200-01-01').status_code == 400)
tk.check('missing time 400', upload(alice, time_str='').status_code == 400)
tk.check('bad time 400', upload(alice, time_str='25:99').status_code == 400)
tk.check('missing photo 400', tk.post(alice, '/api/auto-log', data={
    'date': DAY, 'time': '12:00'},
    content_type='multipart/form-data').status_code == 400)
tk.check('junk image 400 (validated at upload, not in background)',
         upload(alice, photo=b'not an image').status_code == 400)
tk.check('nothing spooled by rejected uploads', spool_files() == [])

# --- Happy path: upload spools, worker commits --------------------------------
stub_openai(GOOD)
resp = upload(alice, time_str='13:37')
tk.check('upload 202 {queued, pending:1}',
         resp.status_code == 202 and resp.get_json() == {'queued': True, 'pending': 1})
tk.check('spool holds one .json + one .jpg pair',
         len(spool_files()) == 2 and spool_files()[0].endswith('.jpg') and
         spool_files()[1].endswith('.json') and
         spool_files()[0][:-4] == spool_files()[1][:-5])
sidecar = json.load(open(os.path.join(config.AUTOLOG_DIR, spool_files()[1])))
tk.check('sidecar carries user/date/time and NO meal content',
         sidecar['user_id'] == UID and sidecar['date'] == DAY and
         sidecar['time'] == '13:37' and
         set(sidecar) <= {'user_id', 'date', 'time', 'uploaded_at', 'attempts'})
tk.check('meal NOT created before processing', meals_on(alice) == [])
tk.check('pending endpoint sees it',
         tk.get(alice, '/api/auto-log/pending').get_json() == {'pending': 1})
tk.check('pending is tenant-scoped (admin sees 0)',
         tk.get(admin, '/api/auto-log/pending').get_json() == {'pending': 0})

uses_before = int(db.get_user(UID).get('ai_uses_today', 0))
tk.check('process_once finishes the entry', autolog.process_once() == 1)
tk.check('spool empty after commit', spool_files() == [])
meals = meals_on(alice)
tk.check('meal created at the EXIF time with the accepted estimate',
         len(meals) == 1 and meals[0]['time'] == '13:37' and
         meals[0]['description'] == 'Dal with brown rice' and
         meals[0]['nutrients']['fiber_g'] == 4.5 and
         meals[0]['ai_assisted'] is True and meals[0]['has_photo'] is True)
tk.check('photo committed to S3 under the canonical key',
         f"users/{UID}/meals/{DAY}/{meals[0]['meal_id']}.jpg" in tk.FIXTURES.s3.objects)
tk.check('one AI use consumed',
         int(db.get_user(UID)['ai_uses_today']) == uses_before + 1)
tk.check('photo proxy serves the committed photo',
         tk.get(alice, meals[0]['photo_url']).status_code == 200)

# --- Daily cap: meal still lands, without an estimate --------------------------
from datetime import datetime, timezone  # noqa: E402
tk.FIXTURES.users.items[(UID,)]['ai_uses_today'] = config.AI_DAILY_LIMIT
tk.FIXTURES.users.items[(UID,)]['ai_uses_date'] = \
    datetime.now(timezone.utc).strftime('%Y-%m-%d')
upload(alice, time_str='19:02')
tk.check('cap: entry still finishes', autolog.process_once() == 1)
meals = meals_on(alice)
capped = [m for m in meals if m['time'] == '19:02']
tk.check('cap: meal saved with placeholder, no estimate, no AI marker',
         len(capped) == 1 and capped[0]['description'] == 'Photo meal — description pending'
         and capped[0]['nutrients'] == {} and capped[0]['ai_assisted'] is False
         and capped[0]['has_photo'] is True)
tk.check('cap: no use consumed past the limit',
         int(db.get_user(UID)['ai_uses_today']) == config.AI_DAILY_LIMIT)
tk.FIXTURES.users.items[(UID,)]['ai_uses_today'] = 0

# --- Upstream failure: refund + deferred retry, then no-estimate fallback ------
stub_openai(exc=ai.requests.RequestException('boom'))
upload(alice, time_str='08:15')
for i in range(autolog.ESTIMATE_ATTEMPTS):
    tk.check(f'upstream failure {i + 1}: entry deferred, not finished',
             autolog.process_once() == 0)
    tk.check(f'upstream failure {i + 1}: use refunded',
             int(db.get_user(UID)['ai_uses_today']) == 0)
tk.check('estimate attempts exhausted: saved without estimate',
         autolog.process_once() == 1)
morning = [m for m in meals_on(alice) if m['time'] == '08:15']
tk.check('fallback meal has placeholder + photo, no estimate',
         len(morning) == 1 and morning[0]['nutrients'] == {} and
         morning[0]['description'] == 'Photo meal — description pending' and
         morning[0]['has_photo'] is True)

# --- Commit failure: estimate cached, not re-billed; dead-letter at the end ----
calls = []
stub_openai(GOOD, calls=calls)
upload(alice, time_str='12:30')
_real_put_meal = db.put_meal
db.put_meal = lambda item: (_ for _ in ()).throw(RuntimeError('ddb down'))
for i in range(autolog.MAX_ATTEMPTS):
    autolog.process_once()
db.put_meal = _real_put_meal
tk.check('commit retries consumed exactly ONE AI call (estimate cached)',
         len(calls) == 1)
tk.check('commit retries consumed exactly one AI use',
         int(db.get_user(UID)['ai_uses_today']) == 1)
dead = [f for f in spool_files() if f.endswith('.json.dead')]
tk.check('exhausted entry dead-lettered (photo kept, not pending)',
         len(dead) == 1 and
         tk.get(alice, '/api/auto-log/pending').get_json() == {'pending': 0})
tk.check('dead-lettered entry no longer processed', autolog.process_once() == 0)
for f in spool_files():
    os.remove(os.path.join(config.AUTOLOG_DIR, f))

# --- Rejected user: spooled photos discarded, never logged ---------------------
stub_openai(GOOD)
upload(alice, time_str='21:00')
tk.post(admin, '/api/admin/users/sub-alice/reject')
tk.check('rejected user entry discarded (finished, no meal)',
         autolog.process_once() == 1 and spool_files() == [])
tk.FIXTURES.users.items[(UID,)]['status'] = 'approved'
tk.check('no meal was written for the rejected window',
         [m for m in meals_on(alice) if m['time'] == '21:00'] == [])

# --- Pending cap ---------------------------------------------------------------
_cap = config.AUTOLOG_MAX_PENDING
config.AUTOLOG_MAX_PENDING = 2
tk.check('uploads under cap accepted',
         upload(alice).status_code == 202 and upload(alice).status_code == 202)
tk.check('cap reached: 429', upload(alice).status_code == 429)
config.AUTOLOG_MAX_PENDING = _cap

# --- Account deletion purges the spool -----------------------------------------
tk.check('two entries pending before deletion',
         tk.get(alice, '/api/auto-log/pending').get_json() == {'pending': 2})
resp = tk.post(alice, '/api/account/delete', json={'confirm': 'delete'})
tk.check('account deletion succeeds with spooled photos', resp.status_code == 200)
tk.check('spool purged with the account', spool_files() == [])

tk.finish('M11 async auto-log')
