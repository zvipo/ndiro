"""M3 exit test (stubbed AWS + Google): review page, share links end to end
(create / read anonymously / revoke / expire, identical 404s, ownership,
caps), and account self-deletion wiping everything.

Run:  python tests/test_m3_shares.py
"""
import io
import time

import testkit as tk

import config
import db

# --- Setup -------------------------------------------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
bob = tk.client()
tk.sign_in(bob, sub='sub-bob', email='bob@example.test', name='Bob')
tk.post(admin, '/api/admin/users/sub-alice/approve')
tk.post(admin, '/api/admin/users/sub-bob/approve')

DAY = '2026-08-05'
resp = tk.post(alice, '/api/meals', data={
    'description': 'Oats with blackberries',
    'context': 'slow morning',
    'date': DAY, 'time': '08:15', 'fiber_g': '3.5',
    'photo': (io.BytesIO(tk.TINY_JPEG), 'photo.jpg'),
}, content_type='multipart/form-data')
assert resp.status_code == 201, resp.get_json()

tk.check('review page renders for approved user', tk.get(alice, '/review').status_code == 200)
tk.check('review page blocked for anonymous', tk.get(tk.client(), '/review').status_code == 302)
tk.check('shares page renders', tk.get(alice, '/shares').status_code == 200)
tk.check('settings page renders', tk.get(alice, '/settings').status_code == 200)

# --- Create a share link -----------------------------------------------------
resp = tk.post(alice, '/api/shares', json={'label': 'Dr. Moyo', 'expires': '7'})
tk.check('share created (201)', resp.status_code == 201)
share = resp.get_json()
token = share['token']
tk.check('share token is long (192-bit urlsafe)', len(token) >= 32)
tk.check('share has expiry in ~7 days',
         abs(share['expires_at'] - (time.time() + 7 * 86400)) < 60)

resp = tk.post(alice, '/api/shares', json={'expires': 'never'})
token_never = resp.get_json()['token']
tk.check('share with no expiry has expires_at null', resp.get_json()['expires_at'] is None)

resp = tk.get(alice, '/api/shares')
tk.check('own shares listed', resp.status_code == 200 and len(resp.get_json()['shares']) == 2)

tk.check('bad expires rejected',
         tk.post(alice, '/api/shares', json={'expires': '13'}).status_code == 400)
tk.check('long label rejected',
         tk.post(alice, '/api/shares', json={'label': 'x' * 101, 'expires': '7'}).status_code == 400)

# --- Anonymous read through the token ----------------------------------------
anon = tk.client()
resp = tk.get(anon, f'/s/{token}')
tk.check('share page renders anonymously', resp.status_code == 200)
tk.check('share page has no edit/AI affordances',
         b'saveBtn' not in resp.data and b'estimateBtn' not in resp.data and
         b'deleteMeal' not in resp.data)
tk.check('share page offers sign-up to anonymous viewers',
         b'/login?next=' in resp.data and b'Sign up' in resp.data)
tk.check('share page attributes the sharer by name',
         b'Shared by' in resp.data and b'Alice' in resp.data)
tk.check('share page never leaks the sharer email',
         b'alice@example.test' not in resp.data)

resp = tk.get(anon, f'/s/{token}/meals?month={DAY[:7]}&anchor={DAY}')
data = resp.get_json()
meals = [m for d in data['days'] for m in d['meals']]
tk.check('share data returns alice meals', resp.status_code == 200 and len(meals) == 1)
tk.check('share photo presigned under ALICE prefix',
         'users/sub-alice/' in (meals[0]['photo_url'] or ''))
tk.check('share meals endpoint rejects POST (read-only)',
         tk.post(anon, f'/s/{token}/meals').status_code == 405)

# --- Share page uses the OWNER's nutrient config, never the viewer's ---------
tk.post(bob, '/api/settings/nutrient', json={
    'preset': 'custom', 'label': 'Iron', 'unit': 'mg', 'goal': 18,
    'direction': 'at_most'})
bob_token = tk.post(bob, '/api/shares', json={'expires': 'never'}).get_json()['token']
page = tk.get(anon, f'/s/{bob_token}')
tk.check("share page shows the owner's custom micro to anonymous viewers",
         page.status_code == 200 and b'Iron' in page.data and b'at_most' in page.data)
page = tk.get(alice, f'/s/{bob_token}')
tk.check("share page shows the owner's micro to a signed-in fiber-default viewer",
         b'Iron' in page.data and b'viscous soluble fiber' not in page.data)
page = tk.get(bob, f'/s/{token}')
tk.check("alice's share still reads as fiber to custom-micro bob",
         b'viscous soluble fiber' in page.data and b'Iron' not in page.data)
tk.delete(bob, f'/api/shares/{bob_token}')
tk.post(bob, '/api/settings/nutrient', json={'preset': 'fiber'})

# --- Ownership: bob cannot revoke alice's link -------------------------------
tk.check("bob revoking alice's share is 404",
         tk.delete(bob, f'/api/shares/{token}').status_code == 404)
tk.check('share still active after foreign revoke attempt',
         tk.get(anon, f'/s/{token}').status_code == 200)

# --- The three failure modes are byte-identical ------------------------------
missing_page = tk.get(anon, '/s/no-such-token-aaaaaaaaaaaaaaaaaaaaa')
tk.check('missing token is 404', missing_page.status_code == 404)

tk.check("alice revokes her share", tk.delete(alice, f'/api/shares/{token}').status_code == 200)
revoked_page = tk.get(anon, f'/s/{token}')
tk.check('revoked token is 404', revoked_page.status_code == 404)

# Expire the 'never' link by editing the stored row directly.
tk.FIXTURES.shares.items[(token_never,)]['expires_at'] = int(time.time()) - 10
expired_page = tk.get(anon, f'/s/{token_never}')
tk.check('expired token is 404', expired_page.status_code == 404)

tk.check('missing/revoked/expired pages byte-identical',
         missing_page.data == revoked_page.data == expired_page.data)
tk.check('data endpoint failure modes identical too',
         tk.get(anon, f'/s/{token}/meals').data ==
         tk.get(anon, '/s/nope/meals').data ==
         tk.get(anon, f'/s/{token_never}/meals').data)

tk.check('revoked/expired rows are kept', len(db.list_user_shares('sub-alice')) == 2)

# --- Active-link cap ---------------------------------------------------------
config.MAX_ACTIVE_SHARES = 2
tk.post(alice, '/api/shares', json={'expires': 'never'})
tk.post(alice, '/api/shares', json={'expires': 'never'})
resp = tk.post(alice, '/api/shares', json={'expires': 'never'})
tk.check('active-share cap enforced (revoked/expired do not count)',
         resp.status_code == 400)
config.MAX_ACTIVE_SHARES = 20

# --- Account self-deletion wipes everything ----------------------------------
live_share = tk.post(alice, '/api/shares', json={'expires': 'never'}).get_json()['token']
tk.check('delete without confirm rejected',
         tk.post(alice, '/api/account/delete', json={}).status_code == 400)
resp = tk.post(alice, '/api/account/delete', json={'confirm': 'delete'})
tk.check('account deletion succeeds', resp.status_code == 200)
tk.check('user row gone', db.get_user('sub-alice') is None)
tk.check('meals gone', db.query_meals_day('sub-alice', DAY) == [])
tk.check('S3 prefix gone',
         not any(k.startswith('users/sub-alice/') for k in tk.FIXTURES.s3.objects))
tk.check('share rows gone', db.list_user_shares('sub-alice') == [])
tk.check('live share link dead after deletion',
         tk.get(anon, f'/s/{live_share}').status_code == 404)
with tk.session(alice) as sess:
    tk.check('session cleared after deletion', 'user_id' not in sess)
resp = tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
tk.check('re-sign-in after deletion starts over as pending',
         resp.status_code == 302 and resp.headers['Location'].endswith('/waiting'))

# --- Share endpoints are rate-limited ----------------------------------------
statuses = [tk.get(anon, '/s/rate-limit-probe').status_code for _ in range(40)]
tk.check('share endpoint rate limit kicks in', 429 in statuses)

tk.finish('M3 review/shares/deletion')
