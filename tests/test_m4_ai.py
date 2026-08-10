"""M4 exit test (stubbed AWS + Google + OpenAI): estimator endpoints, the
race-safe per-user daily cap, refunds on upstream failure, day rollover,
per-IP rate limits, and unset-key degradation.

Run:  python tests/test_m4_ai.py
"""
import io
import json

import testkit as tk

import ai
import config
import db

# --- Setup: one approved user ------------------------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
UID = 'sub-admin'

# --- Unset key => degraded, buttons hidden -----------------------------------
config.OPENAI_API_KEY = None
tk.check('no key: estimate-fiber 400',
         tk.post(admin, '/api/estimate-fiber', json={'description': 'oats'}).status_code == 400)
tk.check('no key: estimate-photo 400',
         tk.post(admin, '/api/estimate-photo', data={
             'photo': (io.BytesIO(b'x'), 'p.jpg')},
             content_type='multipart/form-data').status_code == 400)
tk.check('no key: AI buttons not rendered on /log',
         b'id="estimateBtn"' not in tk.get(admin, '/log').data)
tk.check('no key: no AI use consumed', int(db.get_user(UID).get('ai_uses_today', 0)) == 0)

# --- Enable AI with a stubbed OpenAI backend ---------------------------------
config.OPENAI_API_KEY = 'test-key-not-real'
tk.check('key set: AI buttons rendered on /log',
         b'id="estimateBtn"' in tk.get(admin, '/log').data)

captured = {}


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def stub_openai(result_content=None, status=200, exc=None):
    def fake_post(url, headers=None, json=None, timeout=None):
        captured['url'] = url
        captured['payload'] = json
        captured['timeout'] = timeout
        if exc:
            raise exc
        return FakeResp(status, {
            'choices': [{'message': {'content': result_content}}]})
    ai.requests.post = fake_post


GOOD = json.dumps({
    'viscous_fiber_g': 5.5,
    'items': [{'food': 'Lentils (dal), cooked', 'serving': '1 cup', 'grams': 3.5},
              {'food': 'Carrots, cooked', 'serving': '1 cup', 'grams': 2.0}],
    'note': 'Assumed a full cup of dal.',
})

# --- Happy path: text estimate ----------------------------------------------
stub_openai(GOOD)
resp = tk.post(admin, '/api/estimate-fiber', json={'description': 'dal with carrots'})
tk.check('text estimate 200', resp.status_code == 200)
result = resp.get_json()
tk.check('estimate payload shape',
         result['viscous_fiber_g'] == 5.5 and len(result['items']) == 2 and
         result['model'] == config.OPENAI_MODEL)
tk.check('strict schema requested',
         captured['payload']['response_format']['json_schema']['strict'] is True)
tk.check('guide embedded in system prompt',
         'Psyllium husk powder' in captured['payload']['messages'][0]['content'])
tk.check('one AI use consumed', int(db.get_user(UID)['ai_uses_today']) == 1)

# --- Happy path: photo estimate ----------------------------------------------
GOOD_PHOTO = json.dumps({
    'viscous_fiber_g': 2.0,
    'items': [{'food': 'Broccoli, cooked', 'serving': '1 cup', 'grams': 2.0}],
    'note': '',
    'description': 'Steamed broccoli with rice',
})
stub_openai(GOOD_PHOTO)
resp = tk.post(admin, '/api/estimate-photo', data={
    'photo': (io.BytesIO(b'fake-jpeg'), 'p.jpg')}, content_type='multipart/form-data')
tk.check('photo estimate 200 with description',
         resp.status_code == 200 and resp.get_json()['description'] == 'Steamed broccoli with rice')
tk.check('photo sent as data URL with vision timeout',
         captured['payload']['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
         and captured['timeout'] == (5, 25))
tk.check('scale-reference + no-vessel rules in photo prompt',
         'scale reference' in captured['payload']['messages'][0]['content'] and
         'do not mention plates' in captured['payload']['messages'][0]['content'])
tk.check('two AI uses consumed', int(db.get_user(UID)['ai_uses_today']) == 2)

# --- Validation ---------------------------------------------------------------
tk.check('empty description 400',
         tk.post(admin, '/api/estimate-fiber', json={}).status_code == 400)
tk.check('oversize photo 400',
         tk.post(admin, '/api/estimate-photo', data={
             'photo': (io.BytesIO(b'x' * (8 * 1024 * 1024 + 1)), 'p.jpg')},
             content_type='multipart/form-data').status_code == 400)
tk.check('validation consumed no AI uses', int(db.get_user(UID)['ai_uses_today']) == 2)

# --- Upstream failure => 502 + refund ----------------------------------------
stub_openai(exc=ai.requests.exceptions.ConnectTimeout('boom'))
resp = tk.post(admin, '/api/estimate-fiber', json={'description': 'oats'})
tk.check('upstream timeout => 502', resp.status_code == 502)
tk.check('timeout refunded the use', int(db.get_user(UID)['ai_uses_today']) == 2)

stub_openai('irrelevant', status=500)
resp = tk.post(admin, '/api/estimate-fiber', json={'description': 'oats'})
tk.check('upstream 5xx => 502 + refund',
         resp.status_code == 502 and int(db.get_user(UID)['ai_uses_today']) == 2)

# Unparseable 200 is NOT refunded (the upstream call was billed).
stub_openai('this is not json')
resp = tk.post(admin, '/api/estimate-fiber', json={'description': 'oats'})
tk.check('unreadable 200 => 502, not refunded',
         resp.status_code == 502 and int(db.get_user(UID)['ai_uses_today']) == 3)

# --- Daily cap ----------------------------------------------------------------
tk.limiter.reset()  # clear the per-IP limit so the DAILY cap is what trips
stub_openai(GOOD)
config.AI_DAILY_LIMIT = 5
tk.post(admin, '/api/estimate-fiber', json={'description': 'meal 4'})
tk.post(admin, '/api/estimate-fiber', json={'description': 'meal 5'})
resp = tk.post(admin, '/api/estimate-fiber', json={'description': 'meal 6'})
tk.check('over daily cap => 429 with clear message',
         resp.status_code == 429 and 'Daily AI limit' in resp.get_json()['error'])
tk.check('cap held at limit', int(db.get_user(UID)['ai_uses_today']) == 5)

# Day rollover resets the counter (stored date differs from today).
tk.FIXTURES.users.items[(UID,)]['ai_uses_date'] = '2000-01-01'
resp = tk.post(admin, '/api/estimate-fiber', json={'description': 'new day'})
tk.check('UTC day rollover resets counter to 1',
         resp.status_code == 200 and int(db.get_user(UID)['ai_uses_today']) == 1)
config.AI_DAILY_LIMIT = 10

# --- Direct counter checks (two-call conditional pattern) ---------------------
tk.check('counter: same-day increments under limit',
         db.try_consume_ai_use(UID, db.get_user(UID)['ai_uses_date'], 10) is True)
tk.check('counter: blocks at limit',
         db.try_consume_ai_use(UID, db.get_user(UID)['ai_uses_date'], 2) is False)
db.refund_ai_use(UID, db.get_user(UID)['ai_uses_date'])
tk.check('refund decrements', int(db.get_user(UID)['ai_uses_today']) == 1)
db.refund_ai_use(UID, 'not-today')  # stale-day refund must be a no-op
tk.check('stale-day refund is a no-op', int(db.get_user(UID)['ai_uses_today']) == 1)

# --- Anonymous / pending are blocked ------------------------------------------
tk.limiter.reset()
anon = tk.client()
tk.check('anonymous estimate 401',
         tk.post(anon, '/api/estimate-fiber', json={'description': 'x'}).status_code == 401)

# --- Per-IP rate limit (6/min) LAST — it poisons the endpoint for this IP ----
tk.limiter.reset()
statuses = [tk.post(admin, '/api/estimate-fiber',
                    json={'description': 'rate probe'}).status_code
            for _ in range(10)]
tk.check('per-IP rate limit kicks in', 429 in statuses[6:])

tk.finish('M4 AI estimators + caps')
