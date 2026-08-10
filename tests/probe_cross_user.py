"""M2 exit test — cross-user probe (stubbed AWS + Google).

Proves tenant isolation: user B's session cannot read, edit, or delete user
A's meals through ANY API path, forged form fields included; photos live only
under users/{user_id}/ and are presigned only for their owner.

Run:  python tests/probe_cross_user.py
"""
import io

import testkit as tk

import db

# --- Setup: admin + two approved users ---------------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')

alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
bob = tk.client()
tk.sign_in(bob, sub='sub-bob', email='bob@example.test', name='Bob')
tk.post(admin, '/api/admin/users/sub-alice/approve')
tk.post(admin, '/api/admin/users/sub-bob/approve')

DAY = '2026-08-05'

# --- Alice logs a meal with a photo ------------------------------------------
resp = tk.post(alice, '/api/meals', data={
    'description': 'Lentil dal with brown rice',
    'context': 'post-run, very hungry',
    'date': DAY,
    'time': '12:30',
    'fiber_g': '4.0',
    'photo': (io.BytesIO(tk.TINY_JPEG), 'photo.jpg'),
}, content_type='multipart/form-data')
tk.check('alice creates meal (201)', resp.status_code == 201)
meal = resp.get_json()
meal_id = meal['meal_id']
tk.check('meal time derived from client time', meal['time'] == '12:30')
tk.check('photo stored under users/sub-alice/ prefix',
         list(tk.FIXTURES.s3.objects) == [f'users/sub-alice/meals/{DAY}/{meal_id}.jpg'])
tk.check('photo_url presigned for owner', bool(meal['photo_url']))

resp = tk.get(alice, f'/api/meals?date={DAY}')
data = resp.get_json()
tk.check('alice reads her meal back', len(data['days'][0]['meals']) == 1)
tk.check('alice day total correct', data['days'][0]['totals'].get('fiber_g') == 4.0)

# --- Anonymous probes --------------------------------------------------------
anon = tk.client()
tk.check('anon GET /api/meals is 401', tk.get(anon, f'/api/meals?date={DAY}').status_code == 401)
tk.check('anon DELETE is 401',
         tk.delete(anon, f'/api/meals/{DAY}/{meal_id}').status_code == 401)

# --- Bob probes every API path against Alice's meal --------------------------
resp = tk.get(bob, f'/api/meals?date={DAY}')
tk.check("bob's single-date read shows NO meals",
         resp.get_json()['days'][0]['meals'] == [])

resp = tk.get(bob, f'/api/meals?month={DAY[:7]}&anchor={DAY}')
tk.check("bob's month read shows NO meals",
         all(d['meals'] == [] for d in resp.get_json()['days']))

resp = tk.get(bob, f'/api/meals?days=31&anchor={DAY}')
tk.check("bob's days-window read shows NO meals",
         all(d['meals'] == [] for d in resp.get_json()['days']))

resp = tk.put(bob, f'/api/meals/{DAY}/{meal_id}', data={
    'description': 'hijacked', 'fiber_g': '99'})
tk.check("bob PUT on alice's meal is 404", resp.status_code == 404)

resp = tk.delete(bob, f'/api/meals/{DAY}/{meal_id}')
tk.check("bob DELETE on alice's meal is 404", resp.status_code == 404)

# Forged identity fields in the form must be ignored (session wins).
resp = tk.post(bob, '/api/meals', data={
    'description': 'forged-owner meal',
    'date': DAY,
    'time': '13:00',
    'user_id': 'sub-alice',       # forged
    'sk': f'{DAY}#{meal_id}',     # forged
})
tk.check('forged user_id/sk form fields ignored (meal lands under bob)',
         resp.status_code == 201 and
         db.get_meal('sub-bob', DAY, resp.get_json()['meal_id']) is not None and
         len(db.query_meals_day('sub-alice', DAY)) == 1)

# Bob cannot presign Alice's photo through his own payloads: his meals carry
# no photo, and the defensive presigner refuses foreign prefixes outright.
tk.check('presign refuses foreign-prefix key',
         db.presign_photo(f'users/sub-alice/meals/{DAY}/{meal_id}.jpg', 'sub-bob') is None)

# --- Alice's data survived all probes intact ---------------------------------
row = db.get_meal('sub-alice', DAY, meal_id)
tk.check("alice's meal unchanged after probes",
         row and row['description'] == 'Lentil dal with brown rice' and
         float(row['nutrients']['fiber_g']) == 4.0)
tk.check("alice's photo object still present",
         f'users/sub-alice/meals/{DAY}/{meal_id}.jpg' in tk.FIXTURES.s3.objects)

# --- Owner paths still work (control group) ----------------------------------
resp = tk.put(alice, f'/api/meals/{DAY}/{meal_id}', data={
    'description': 'Lentil dal with extra spinach', 'fiber_g': '4.5'})
tk.check('alice can edit her meal', resp.status_code == 200)
tk.check('edit preserved created_at',
         resp.get_json()['created_at'] == meal['created_at'])

resp = tk.delete(alice, f'/api/meals/{DAY}/{meal_id}')
tk.check('alice can delete her meal', resp.status_code == 200)
tk.check('photo object deleted with meal',
         f'users/sub-alice/meals/{DAY}/{meal_id}.jpg' not in tk.FIXTURES.s3.objects)

# --- Payload-shape sanity (month axis, ordering, validation) -----------------
tk.post(alice, '/api/meals', data={'description': 'breakfast', 'date': '2026-08-02', 'time': '08:00'})
tk.post(alice, '/api/meals', data={'description': 'dinner', 'date': '2026-08-02', 'time': '19:00'})
resp = tk.get(alice, '/api/meals?month=2026-08&anchor=2026-08-05')
data = resp.get_json()
tk.check('month payload spans full range newest-first, empty days included',
         [d['date'] for d in data['days']] ==
         ['2026-08-05', '2026-08-04', '2026-08-03', '2026-08-02', '2026-08-01'])
day2 = next(d for d in data['days'] if d['date'] == '2026-08-02')
tk.check('meals chronological within a day',
         [m['description'] for m in day2['meals']] == ['breakfast', 'dinner'])
tk.check('future month rejected',
         tk.get(alice, '/api/meals?month=2026-09&anchor=2026-08-05').status_code == 400)
tk.check('missing date on create rejected',
         tk.post(alice, '/api/meals', data={'description': 'no date'}).status_code == 400)
tk.check('negative fiber rejected',
         tk.post(alice, '/api/meals', data={
             'description': 'x', 'date': DAY, 'fiber_g': '-1'}).status_code == 400)

# --- Nutrient settings are tenant-isolated too -------------------------------
# The endpoint takes no user_id anywhere; forged identity fields are ignored
# and only the session user's row changes.
resp = tk.post(bob, '/api/settings/nutrient', json={
    'preset': 'custom', 'label': 'Iron', 'unit': 'mg', 'goal': 18,
    'direction': 'at_most', 'user_id': 'sub-alice'})  # forged field ignored
tk.check("bob's nutrient config lands on bob only",
         resp.status_code == 200 and
         db.get_user('sub-bob').get('nutrient_key') == 'iron_mg' and
         'nutrient_key' not in db.get_user('sub-alice'))

# Each user's meal writes use their OWN resolved key: bob logging 'iron_mg'
# while alice (default) posts the same field name stores nothing for alice.
resp = tk.post(bob, '/api/meals', data={
    'description': 'bob iron meal', 'date': DAY, 'time': '14:00', 'iron_mg': '6'})
tk.check("bob's meal stored under his own key",
         resp.status_code == 201 and resp.get_json()['nutrients'] == {'iron_mg': 6.0})
resp = tk.post(alice, '/api/meals', data={
    'description': 'alice probe meal', 'date': DAY, 'time': '15:00', 'iron_mg': '6'})
tk.check("alice (fiber default) posting iron_mg stores no nutrients",
         resp.status_code == 201 and resp.get_json()['nutrients'] == {})

# Pending users cannot touch the meal APIs.
carol = tk.client()
tk.sign_in(carol, sub='sub-carol', email='carol@example.test', name='Carol')
tk.check('pending user blocked from meals API (403)',
         tk.get(carol, f'/api/meals?date={DAY}').status_code == 403)

tk.finish('M2 cross-user probe')
