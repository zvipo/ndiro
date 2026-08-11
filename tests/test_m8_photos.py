"""M8 exit test (stubbed AWS + Google): the photo proxy + cache — bytes and
headers, LRU cache hits vs S3 round-trips, 304 conditional flow, version
busting on replace, tenant/share scoping, deletion hygiene, rate limits, and
_PhotoCache unit behavior.

Run:  python tests/test_m8_photos.py
"""
import io

import testkit as tk

import db

# --- Setup: approved user with a photo meal -----------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
tk.post(admin, '/api/admin/users/sub-alice/approve')

DAY = '2026-08-05'
resp = tk.post(alice, '/api/meals', data={
    'description': 'Oats with berries', 'date': DAY, 'time': '08:15',
    'fiber_g': '4.0', 'photo': (io.BytesIO(tk.TINY_JPEG), 'p.jpg'),
}, content_type='multipart/form-data')
assert resp.status_code == 201, resp.get_json()
meal = resp.get_json()
meal_id = meal['meal_id']
S3_KEY = f'users/sub-alice/meals/{DAY}/{meal_id}.jpg'
url_v1 = meal['photo_url']

# --- Proxy 200: bytes + headers ----------------------------------------------
resp = tk.get(alice, url_v1)
tk.check('proxy serves the exact stored JPEG bytes',
         resp.status_code == 200 and resp.mimetype == 'image/jpeg' and
         resp.data == tk.FIXTURES.s3.objects[S3_KEY])
etag = resp.headers.get('ETag')
tk.check('response carries an ETag matching the URL version',
         etag is not None and etag.strip('"') == url_v1.split('?v=')[1])
cc = resp.headers.get('Cache-Control', '')
tk.check('owner Cache-Control is private + immutable + long-lived',
         'private' in cc and 'immutable' in cc and 'max-age=31536000' in cc)

# --- Cache: second read never touches S3 --------------------------------------
calls_before = tk.FIXTURES.s3.get_calls
tk.get(alice, url_v1)
tk.check('repeat fetch is served from the LRU (no S3 round-trip)',
         tk.FIXTURES.s3.get_calls == calls_before)

# --- Conditional flow ----------------------------------------------------------
resp = tk.get(alice, url_v1, headers={'If-None-Match': etag})
tk.check('If-None-Match returns 304 with an empty body',
         resp.status_code == 304 and resp.data == b'' and
         tk.FIXTURES.s3.get_calls == calls_before)
# Weak validators (Cloudflare downgrades ETags to W/"..." when transforming)
# must ALSO 304, or every daily share revalidation re-downloads the image.
weak = f'W/{etag}'
resp = tk.get(alice, url_v1, headers={'If-None-Match': weak})
tk.check('weak If-None-Match also returns 304', resp.status_code == 304)

# --- Stale-version URLs are dead, never "current bytes" -------------------------
resp = tk.get(alice, url_v1.split('?')[0] + '?v=deadbeefdeadbeef')
tk.check('mismatched ?v= is 404 (immutable URLs never serve other bytes)',
         resp.status_code == 404)
tk.check('version-less URL still serves (legacy/manual fetches)',
         tk.get(alice, url_v1.split('?')[0]).status_code == 200)

# --- HEAD ----------------------------------------------------------------------
resp = alice.head(url_v1, base_url=tk.BASE_URL)
tk.check('HEAD returns headers with an empty body',
         resp.status_code == 200 and resp.data == b'' and
         'private' in resp.headers.get('Cache-Control', ''))

# --- Text-only edits do NOT bust the photo version ------------------------------
resp = tk.put(alice, f'/api/meals/{DAY}/{meal_id}', data={
    'description': 'Oats with berries and a typo fix', 'fiber_g': '4.0'})
tk.check('text-only edit keeps the SAME photo URL (photo_v stable)',
         resp.status_code == 200 and resp.get_json()['photo_url'] == url_v1)
calls_before = tk.FIXTURES.s3.get_calls
tk.get(alice, url_v1)
tk.check('text-only edit did not evict the cached bytes',
         tk.FIXTURES.s3.get_calls == calls_before)

# --- Replace busts the version --------------------------------------------------
NEW_JPEG = tk.TINY_JPEG + b'\x00'  # distinct bytes, still parseable tail-junk JPEG
resp = tk.put(alice, f'/api/meals/{DAY}/{meal_id}', data={
    'description': 'Oats with berries', 'fiber_g': '4.0',
    'photo': (io.BytesIO(NEW_JPEG), 'p2.jpg'),
}, content_type='multipart/form-data')
url_v2 = resp.get_json()['photo_url']
tk.check('photo replace yields a NEW versioned URL (same path, new ?v=)',
         resp.status_code == 200 and url_v2 != url_v1 and
         url_v2.split('?')[0] == url_v1.split('?')[0])
calls_before = tk.FIXTURES.s3.get_calls
resp = tk.get(alice, url_v2)
tk.check('new version misses the cache and serves the NEW bytes',
         resp.data == tk.FIXTURES.s3.objects[S3_KEY] and
         tk.FIXTURES.s3.get_calls == calls_before + 1)

# --- Remove photo ---------------------------------------------------------------
resp = tk.put(alice, f'/api/meals/{DAY}/{meal_id}', data={
    'description': 'Oats with berries', 'fiber_g': '4.0', 'remove_photo': '1'})
tk.check('remove_photo nulls the photo_url', resp.get_json()['photo_url'] is None)
tk.check('old photo URL now 404s', tk.get(alice, url_v2).status_code == 404)
tk.check('cache purged on photo delete',
         not any(k[0] == S3_KEY for k in db._photo_cache._items))

# --- 404 uniformity -------------------------------------------------------------
no_photo = tk.get(alice, f'/photo/{DAY}/{meal_id}')       # meal exists, no photo
no_meal = tk.get(alice, f'/photo/{DAY}/000000-ffffff')    # no such meal
bad_date = tk.get(alice, f'/photo/2026-8-5/{meal_id}')    # invalid date
tk.check('no-photo / no-meal / bad-date collapse to one identical 404',
         no_photo.status_code == no_meal.status_code == bad_date.status_code == 404
         and no_photo.data == no_meal.data == bad_date.data)

# --- Share scoping ---------------------------------------------------------------
resp = tk.post(alice, '/api/meals', data={
    'description': 'Lunch', 'date': DAY, 'time': '13:00',
    'photo': (io.BytesIO(tk.TINY_JPEG), 'p.jpg')}, content_type='multipart/form-data')
meal2_id = resp.get_json()['meal_id']
share_token = tk.post(alice, '/api/shares', json={'expires': 'never'}).get_json()['token']
data = tk.get(tk.client(), f'/s/{share_token}/meals?month={DAY[:7]}&anchor={DAY}').get_json()
share_meal = [m for d in data['days'] for m in d['meals'] if m['meal_id'] == meal2_id][0]
anon = tk.client()
resp = tk.get(anon, share_meal['photo_url'])
tk.check('anon share photo fetch works through the token route',
         resp.status_code == 200 and resp.mimetype == 'image/jpeg')
tk.check('share photo max-age is one day (revocation-bounded), still private',
         'max-age=86400' in resp.headers.get('Cache-Control', '') and
         'private' in resp.headers.get('Cache-Control', ''))
bob = tk.client()
tk.sign_in(bob, sub='sub-bob', email='bob@example.test', name='Bob')
tk.post(admin, '/api/admin/users/sub-bob/approve')
tk.check('signed-in third party can use the share photo route',
         tk.get(bob, share_meal['photo_url']).status_code == 200)
tk.check("but bob's own /photo/ path for alice's meal is 404",
         tk.get(bob, f'/photo/{DAY}/{meal2_id}').status_code == 404)

# --- Deletion hygiene ------------------------------------------------------------
tk.delete(alice, f'/api/meals/{DAY}/{meal2_id}')
tk.check('meal delete purges the photo from cache and the URL 404s',
         tk.get(alice, f'/photo/{DAY}/{meal2_id}').status_code == 404 and
         not any(k[0].endswith(f'{meal2_id}.jpg') for k in db._photo_cache._items))

# --- _PhotoCache unit behavior ----------------------------------------------------
c = db._PhotoCache(100)
c.put('users/u/a.jpg', 'v1', b'x' * 40)
c.put('users/u/b.jpg', 'v1', b'y' * 40)
c.get('users/u/a.jpg', 'v1')                      # a becomes most-recent
c.put('users/u/c.jpg', 'v1', b'z' * 40)           # evicts b (LRU head)
tk.check('LRU evicts the least-recently-used entry',
         c.get('users/u/b.jpg', 'v1') is None and
         c.get('users/u/a.jpg', 'v1') is not None and
         c.get('users/u/c.jpg', 'v1') is not None)
c.put('users/u/huge.jpg', 'v1', b'h' * 101)
tk.check('oversize objects are never cached', c.get('users/u/huge.jpg', 'v1') is None)
c.put('users/u/a.jpg', 'v2', b'x' * 40)
tk.check('a new version drops the old one',
         c.get('users/u/a.jpg', 'v1') is None and
         c.get('users/u/a.jpg', 'v2') is not None)
c.drop_prefix('users/u/')
tk.check('drop_prefix clears the subtree and its accounting',
         c.get('users/u/a.jpg', 'v2') is None and c._size == 0)

# --- No bucket => no photo_url (S3_BUCKET is documented optional) ----------------
import config
resp = tk.post(alice, '/api/meals', data={
    'description': 'Dinner', 'date': DAY, 'time': '19:00',
    'photo': (io.BytesIO(tk.TINY_JPEG), 'p.jpg')}, content_type='multipart/form-data')
meal3_id = resp.get_json()['meal_id']
_bucket = config.S3_BUCKET
config.S3_BUCKET = None
resp = tk.get(alice, f'/api/meals?date={DAY}&anchor={DAY}')
m = [x for d in resp.get_json()['days'] for x in d['meals'] if x['meal_id'] == meal3_id][0]
tk.check('bucketless deploys emit has_photo but NO unresolvable photo_url',
         m['has_photo'] is True and m['photo_url'] is None)
config.S3_BUCKET = _bucket

# --- Rate limit -------------------------------------------------------------------
tk.limiter.reset()
statuses = [tk.get(alice, f'/photo/{DAY}/{meal_id}').status_code for _ in range(610)]
tk.check('photo route rate limit kicks in past 600/min', 429 in statuses)

tk.finish('M8 photo proxy + cache')
