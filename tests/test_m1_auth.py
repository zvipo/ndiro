"""M1 exit test (stubbed AWS + Google): sign-up statuses, approval flow,
rejection killing live sessions, MAX_USERS enforcement.

Run:  python tests/test_m1_auth.py
"""
import testkit as tk

import config
import db

# --- 1. Admin email bootstraps as admin, lands approved ----------------------
admin = tk.client()
resp = tk.sign_in(admin, sub='sub-admin-1', email='admin@example.test', name='Admin')
tk.check('admin sign-in redirects to /log', resp.status_code == 302 and resp.headers['Location'].endswith('/log'))
row = db.get_user('sub-admin-1')
tk.check('admin row has status=admin', row and row['status'] == 'admin')
tk.check('admin row has approved_at', bool(row.get('approved_at')))
tk.check('admin can open /admin', tk.get(admin, '/admin').status_code == 200)
tk.check('admin users API works', tk.get(admin, '/api/admin/users').status_code == 200)

# --- 2. Second account -> pending -> waiting page ----------------------------
bob = tk.client()
resp = tk.sign_in(bob, sub='sub-bob-2', email='bob@example.test', name='Bob')
tk.check('new user redirected to /waiting', resp.status_code == 302 and resp.headers['Location'].endswith('/waiting'))
row = db.get_user('sub-bob-2')
tk.check('new user row is pending', row and row['status'] == 'pending')
tk.check('waiting page renders for pending', tk.get(bob, '/waiting').status_code == 200)
tk.check('pending user blocked from /admin page', tk.get(bob, '/admin').status_code == 302)
tk.check('pending user blocked from admin API', tk.get(bob, '/api/admin/users').status_code == 403)

# --- 3. Admin approves -> user is in -----------------------------------------
resp = tk.post(admin, '/api/admin/users/sub-bob-2/approve')
tk.check('approve API 200', resp.status_code == 200)
row = db.get_user('sub-bob-2')
tk.check('user now approved with approved_at', row['status'] == 'approved' and row.get('approved_at'))
resp = tk.get(bob, '/waiting')
tk.check('approved user bounced from /waiting to /log', resp.status_code == 302 and resp.headers['Location'].endswith('/log'))

# --- 4. Rejection kills the LIVE session immediately -------------------------
resp = tk.post(admin, '/api/admin/users/sub-bob-2/reject')
tk.check('reject API 200', resp.status_code == 200)
resp = tk.get(bob, '/waiting')
tk.check('rejected user bounced to /', resp.status_code == 302 and resp.headers['Location'] in ('/', tk.BASE_URL + '/'))
with tk.session(bob) as sess:
    tk.check('rejected user session cleared', 'user_id' not in sess)
tk.check('rejected user API access is 401', tk.get(bob, '/api/admin/users').status_code == 401)

# Rejected user signing in again gets no session back.
resp = tk.sign_in(bob, sub='sub-bob-2', email='bob@example.test', name='Bob')
with tk.session(bob) as sess:
    tk.check('rejected re-sign-in gets no session', 'user_id' not in sess)

# --- 5. Admins cannot be modified via the approve/reject API -----------------
resp = tk.post(admin, '/api/admin/users/sub-admin-1/reject')
tk.check('admin rows immutable via API', resp.status_code == 400)

# --- 6. MAX_USERS enforced server-side at creation ---------------------------
config.MAX_USERS = 2  # admin + bob already exist
carol = tk.client()
resp = tk.sign_in(carol, sub='sub-carol-3', email='carol@example.test', name='Carol')
tk.check('at capacity: sign-up blocked with 403', resp.status_code == 403)
tk.check('at capacity: no user row created', db.get_user('sub-carol-3') is None)
config.MAX_USERS = 100

# --- 7. Misc auth hygiene ----------------------------------------------------
fresh = tk.client()
resp = tk.get(fresh, '/callback?state=forged&code=x')
tk.check('callback rejects bad state', resp.status_code == 400)
resp = tk.get(fresh, '/logout?next=//evil.example')
tk.check('logout open-redirect blocked', resp.headers['Location'] in ('/', tk.BASE_URL + '/'))
resp = tk.get(fresh, '/')
tk.check('landing renders anonymously', resp.status_code == 200)
resp = tk.get(fresh, '/privacy')
tk.check('privacy page public', resp.status_code == 200)
tk.check('health endpoint public', tk.get(fresh, '/health').status_code == 200)

tk.finish('M1 auth/approval')
