"""M12: native (email/password) accounts.

Covers: the /login chooser page, signup -> email verification -> session,
single-use/expiring tokens with byte-identical dead pages, invites claimed
only at verification, MAX_USERS before invite logic, duplicate-email
anti-enumeration, the uniform sign-in error (lockout included), password
reset, form-token CSRF, the settings password change, and that hashes/tokens
never leak into the admin payload or emails' storage.

Run:  python tests/test_m12_native.py
"""
import time

import testkit as tk

import config
import db
import native_auth

M = tk.MAILER

ADMIN = tk.client()
tk.sign_in(ADMIN, sub='sub-admin', email='admin@example.test', name='Admin')


def approve(user_id):
    resp = tk.post(ADMIN, f'/api/admin/users/{user_id}/approve')
    assert resp.status_code == 200, resp.status_code


# --- 1. The /login chooser page ----------------------------------------------
anon = tk.client()
body = tk.get(anon, '/login').get_data(as_text=True)
tk.check('login page renders both paths',
         '/login/google' in body and '/login/password' in body
         and '/signup' in body and '/forgot' in body)

saved_client_id = config.GOOGLE_CLIENT_ID
config.GOOGLE_CLIENT_ID = None
body = tk.get(tk.client(), '/login').get_data(as_text=True)
tk.check('google button absent when unconfigured',
         '/login/google' not in body and '/login/password' in body)
tk.check('/login/google 503 when unconfigured',
         tk.get(tk.client(), '/login/google').status_code == 503)
config.GOOGLE_CLIENT_ID = saved_client_id

saved_mail_from = config.MAIL_FROM
config.MAIL_FROM = None
config.EMAIL_ENABLED = False
body = tk.get(tk.client(), '/login').get_data(as_text=True)
tk.check('signup/forgot links absent when email disabled',
         '/signup' not in body and '/forgot' not in body)
tk.check('signup 503 when email disabled',
         tk.get(tk.client(), '/signup').status_code == 503)
tk.check('forgot 503 when email disabled',
         tk.get(tk.client(), '/forgot').status_code == 503)
tk.check('password sign-in form still renders when email disabled',
         '/login/password' in body)
config.MAIL_FROM = saved_mail_from
config.EMAIL_ENABLED = True
tk.limiter.reset()

# --- 2. Signup -> verify -> session ------------------------------------------
c = tk.client()
mails_before = len(M.sent)
resp = tk.native_signup(c, 'alice@example.test', 'hunter2hunter2', name='Alice')
tk.check('signup shows the check-email page',
         resp.status_code == 200 and b'Check your email' in resp.data)
tk.check('signup sent exactly one mail', len(M.sent) == mails_before + 1)
tk.check('verification mail goes to the address',
         M.sent[-1][0] == 'alice@example.test' and 'Verify' in M.sent[-1][1])
with tk.session(c) as sess:
    tk.check('no session before verification', 'user_id' not in sess)

alice = db.find_user_by_email('alice@example.test')
tk.check('signup row is native + pending + unverified',
         alice is not None and alice['auth_provider'] == 'native'
         and alice['status'] == 'pending' and not alice['email_verified'])
tk.check('row stores a token HASH, not the link token',
         tk.extract_link(M.sent[-1][2]).rsplit('/', 1)[-1]
         != alice['verify_token_hash'])

verify_path = tk.extract_link(M.sent[-1][2])
tk.check('mail links a verify path', verify_path.startswith('/verify-email/'))

resp = tk.get(c, verify_path)
tk.check('verify GET shows a confirm page (non-consuming)',
         resp.status_code == 200 and b'Verify my email' in resp.data)
resp = tk.get(c, verify_path)
tk.check('verify GET repeatable (still not consumed)', resp.status_code == 200)

resp = tk.post(c, verify_path)
tk.check('verify POST signs in and redirects to /waiting',
         resp.status_code == 302 and resp.headers['Location'].endswith('/waiting'))
alice = db.get_user(alice['user_id'])
tk.check('row now verified with token cleared',
         alice['email_verified'] and 'verify_token_hash' not in alice)
with tk.session(c) as sess:
    tk.check('post-verify session holds only user_id',
             set(sess.keys()) <= {'user_id', '_permanent'})

dead = tk.post(tk.client(), verify_path)
tk.check('verify link is single-use', dead.status_code == 404)

approve(alice['user_id'])
resp = tk.get(c, '/log')
tk.check('approved native user reaches /log', resp.status_code == 200)
tk.limiter.reset()

# --- 3. Verify-link expiry + byte-identical dead pages -----------------------
c2 = tk.client()
tk.native_signup(c2, 'bob@example.test', 'correct-horse-battery')
bob_link = tk.extract_link(M.sent[-1][2])
bob = db.find_user_by_email('bob@example.test')
tk.check('a blank name stays blank (never the email local-part)',
         bob['name'] == '')
tk.FIXTURES.users.items[(bob['user_id'],)]['verify_expires_at'] = 1
expired = tk.get(c2, bob_link)
tk.check('expired verify link is dead', expired.status_code == 404)
missing = tk.get(c2, '/verify-email/no-such-token')
tk.check('missing and expired links are byte-identical',
         expired.data == missing.data)
tk.check('used and missing links are byte-identical', dead.data == missing.data)
tk.FIXTURES.users.items[(bob['user_id'],)]['verify_expires_at'] = \
    int(time.time()) + 3600
tk.limiter.reset()

# --- 4. Unverified + correct password -> verify-needed page ------------------
resp = tk.post(c2, '/login/password',
               data={'form_token': tk.form_token(c2), 'next': '/log',
                     'email': 'bob@example.test',
                     'password': 'correct-horse-battery'})
tk.check('correct password on unverified account points at verification',
         resp.status_code == 200 and b'Verify your email first' in resp.data)
with tk.session(c2) as sess:
    tk.check('...and creates no session', 'user_id' not in sess)

mails_before = len(M.sent)
resp = tk.post(c2, '/resend-verification',
               data={'form_token': tk.form_token(c2),
                     'email': 'bob@example.test'})
tk.check('resend re-mints and sends', resp.status_code == 200
         and len(M.sent) == mails_before + 1)
tk.check('old verify link dead after resend',
         tk.get(tk.client(), bob_link).status_code == 404)
resp = tk.post(c2, tk.extract_link(M.sent[-1][2]))
tk.check('re-minted link verifies', resp.status_code == 302)

mails_before = len(M.sent)
resp = tk.post(tk.client(), '/resend-verification',
               data={'email': 'bob@example.test'})
tk.check('resend without a form token 400', resp.status_code == 400)
resp = tk.post(c2, '/resend-verification',
               data={'form_token': tk.form_token(c2),
                     'email': 'nobody-here@example.test'})
tk.check('resend for unknown address: same page, no mail',
         resp.status_code == 200 and b'Check your email' in resp.data
         and len(M.sent) == mails_before)
tk.limiter.reset()

# --- 5. Uniform sign-in errors + lockout -------------------------------------
def try_login(client, email, password):
    return tk.post(client, '/login/password',
                   data={'form_token': tk.form_token(client), 'next': '/log',
                         'email': email, 'password': password})

# One probing client throughout: its session form_token is stable, so any
# difference between the re-rendered pages would be an account-state oracle.
probe = tk.client()
wrong = try_login(probe, 'alice@example.test', 'not-the-password')
unknown = try_login(probe, 'ghost@example.test', 'not-the-password')
tk.check('wrong password and unknown email errors are uniform',
         wrong.status_code == unknown.status_code == 200
         and wrong.data.replace(b'alice@example.test', b'X')
         == unknown.data.replace(b'ghost@example.test', b'X'))
tk.check('google-account email cannot password-sign-in (same error)',
         try_login(probe, 'admin@example.test', 'whatever-pw').data
         .replace(b'admin@example.test', b'X')
         == unknown.data.replace(b'ghost@example.test', b'X'))

for i in range(native_auth.LOCKOUT_THRESHOLD - 1):
    tk.limiter.reset()
    try_login(tk.client(), 'alice@example.test', 'not-the-password')
alice = db.get_user(alice['user_id'])
tk.check('failure counter tracks consecutive misses',
         int(alice.get('failed_logins', 0)) == native_auth.LOCKOUT_THRESHOLD
         and int(alice.get('locked_until', 0)) > time.time())
tk.limiter.reset()
locked = try_login(probe, 'alice@example.test', 'hunter2hunter2')
tk.check('locked account gets the SAME error, even with the right password',
         locked.data.replace(b'alice@example.test', b'X')
         == unknown.data.replace(b'ghost@example.test', b'X'))
tk.FIXTURES.users.items[(alice['user_id'],)]['locked_until'] = 1

tk.limiter.reset()
c3 = tk.client()
resp = try_login(c3, 'ALICE@example.test', 'hunter2hunter2')
tk.check('sign-in works (case-insensitive email) once unlocked',
         resp.status_code == 302 and resp.headers['Location'].endswith('/log'))
alice = db.get_user(alice['user_id'])
tk.check('success clears the failure counter',
         'failed_logins' not in alice and 'locked_until' not in alice)
with tk.session(c3) as sess:
    tk.check('password sign-in session holds only user_id',
             set(sess.keys()) <= {'user_id', '_permanent'})

tk.post(ADMIN, f"/api/admin/users/{alice['user_id']}/reject")
resp = tk.get(c3, '/log')
tk.check('rejection kills the live native session', resp.status_code == 302)
tk.limiter.reset()
resp = try_login(tk.client(), 'alice@example.test', 'hunter2hunter2')
tk.check('rejected native user gets no session back',
         resp.status_code == 302 and resp.headers['Location'].endswith('/'))
tk.post(ADMIN, f"/api/admin/users/{alice['user_id']}/approve")
# Re-establish alice's session (the rejection killed it above).
tk.limiter.reset()
try_login(c3, 'alice@example.test', 'hunter2hunter2')
tk.limiter.reset()

# --- 6. Password reset -------------------------------------------------------
mails_before = len(M.sent)
resp = tk.post(c3, '/forgot', data={'form_token': tk.form_token(c3),
                                    'email': 'alice@example.test'})
tk.check('forgot shows the uniform sent page',
         resp.status_code == 200 and b'Check your email' in resp.data)
tk.check('forgot sent one reset mail', len(M.sent) == mails_before + 1
         and 'Reset' in M.sent[-1][1])
reset_path = tk.extract_link(M.sent[-1][2])
tk.check('reset link path', reset_path.startswith('/reset/'))

resp = tk.get(c3, reset_path)
tk.check('reset GET shows the form (non-consuming)',
         resp.status_code == 200 and b'Set a new password' in resp.data)
resp = tk.post(c3, reset_path, data={'password': 'brand-new-pw-99',
                                     'password2': 'nope'})
tk.check('mismatched repeat rejected, token intact',
         resp.status_code == 200 and b'do not match' in resp.data)
resp = tk.post(c3, reset_path, data={'password': 'brand-new-pw-99',
                                     'password2': 'brand-new-pw-99'})
tk.check('reset POST completes to the sign-in page',
         resp.status_code == 302 and '/login?reset=1' in resp.headers['Location'])
tk.check('reset link is single-use',
         tk.post(tk.client(), reset_path,
                 data={'password': 'x' * 12, 'password2': 'x' * 12}).status_code == 404)
tk.limiter.reset()
tk.check('old password dead after reset',
         b'Wrong email or password.' in
         try_login(tk.client(), 'alice@example.test', 'hunter2hunter2').data)
tk.limiter.reset()
tk.check('new password works',
         try_login(tk.client(), 'alice@example.test', 'brand-new-pw-99')
         .status_code == 302)

# Forgot for the other account states: uniform page, distinct mail (or none).
tk.limiter.reset()
mails_before = len(M.sent)
tk.post(c3, '/forgot', data={'form_token': tk.form_token(c3),
                             'email': 'admin@example.test'})
tk.check('forgot for a google email sends the google reset notice',
         len(M.sent) == mails_before + 1
         and 'password reset' in M.sent[-1][1]
         and 'signs in with Google' in M.sent[-1][2])
mails_before = len(M.sent)
resp = tk.post(c3, '/forgot', data={'form_token': tk.form_token(c3),
                                    'email': 'ghost@example.test'})
tk.check('forgot for an unknown email: same page, no mail',
         b'Check your email' in resp.data and len(M.sent) == mails_before)
tk.limiter.reset()

# --- 7. Signup duplicates: no enumeration, no second row ---------------------
users_before = len(tk.FIXTURES.users.items)
mails_before = len(M.sent)
dup_probe = tk.client()  # same client both times: stable form_token in the page
resp_dup = tk.native_signup(dup_probe, 'alice@example.test', 'password-123')
tk.limiter.reset()
resp_new = tk.native_signup(dup_probe, 'carol@example.test', 'password-123')
tk.check('duplicate signup renders the SAME check-email page',
         resp_dup.status_code == resp_new.status_code == 200
         and resp_dup.data.replace(b'alice@example.test', b'X')
         == resp_new.data.replace(b'carol@example.test', b'X'))
tk.check('duplicate signup creates no row, notice mail sent instead',
         len(tk.FIXTURES.users.items) == users_before + 1  # carol only
         and any(m[0] == 'alice@example.test' and 'already have' in m[1]
                 for m in M.sent[mails_before:]))
tk.limiter.reset()
resp = tk.native_signup(tk.client(), 'admin@example.test', 'password-123')
tk.check('signup with an ADMIN_EMAILS address never bootstraps admin',
         b'Check your email' in resp.data
         and db.find_user_by_email('admin@example.test')['status'] == 'admin'
         and db.find_user_by_email('admin@example.test', provider='native') is None)

# Stale unverified rows stop squatting the email once their token expires.
# (The user_id is email-derived, so the fresh row reuses the same key — the
# re-minted verify token is what proves the purge-and-recreate happened.)
carol = db.find_user_by_email('carol@example.test')
tk.FIXTURES.users.items[(carol['user_id'],)]['verify_expires_at'] = 1
tk.limiter.reset()
resp = tk.native_signup(tk.client(), 'carol@example.test', 'password-456')
carol2 = db.find_user_by_email('carol@example.test')
tk.check('expired unverified row purged and re-signup proceeds',
         b'Check your email' in resp.data and carol2 is not None
         and carol2['verify_token_hash'] != carol['verify_token_hash']
         and carol2['created_at'] != carol['created_at'])
tk.limiter.reset()

# --- 8. Invites: validated at signup, claimed only at verification -----------
resp = tk.post(c3, '/api/invites', json={})  # c3 = alice, approved
tk.check('inviter can mint an invite', resp.status_code == 201)
inv_token = resp.get_json()['token']

c4 = tk.client()
tk.native_signup(c4, 'dave@example.test', 'password-789', invite=inv_token)
inv_row = tk.FIXTURES.invites.items[(inv_token,)]
tk.check('invite NOT consumed at signup', 'used_by' not in inv_row)
dave = db.find_user_by_email('dave@example.test')
tk.check('pending invite rides the user row',
         dave.get('pending_invite_token') == inv_token
         and dave['status'] == 'pending')

resp = tk.post(c4, tk.extract_link(M.sent[-1][2]))
dave = db.get_user(dave['user_id'])
inv_row = tk.FIXTURES.invites.items[(inv_token,)]
tk.check('verification claims the invite and approves the account',
         resp.status_code == 302 and resp.headers['Location'].endswith('/log')
         and dave['status'] == 'approved'
         and dave.get('invited_by') == alice['user_id']
         and inv_row.get('used_by') == dave['user_id'])
tk.check('pending_invite_token cleared after verification',
         'pending_invite_token' not in dave)
tk.limiter.reset()

# MAX_USERS beats invite logic: full instance burns nothing.
resp = tk.post(c3, '/api/invites', json={})
tok_full = resp.get_json()['token']
saved_max = config.MAX_USERS
config.MAX_USERS = db.count_users()
resp = tk.native_signup(tk.client(), 'eve@example.test', 'password-000',
                        invite=tok_full)
tk.check('full instance: signup 403, invite untouched, no row',
         resp.status_code == 403
         and 'used_by' not in tk.FIXTURES.invites.items[(tok_full,)]
         and db.find_user_by_email('eve@example.test') is None)
config.MAX_USERS = saved_max
tk.limiter.reset()

# --- 9. Form-token CSRF on the unauthenticated POSTs -------------------------
naked = tk.client()
tk.check('login POST without form token 400',
         tk.post(naked, '/login/password',
                 data={'email': 'a@b.co', 'password': 'x' * 10}).status_code == 400)
tk.check('signup POST without form token 400',
         tk.post(naked, '/signup',
                 data={'email': 'a@b.co', 'password': 'x' * 10}).status_code == 400)
tk.check('forgot POST without form token 400',
         tk.post(naked, '/forgot', data={'email': 'a@b.co'}).status_code == 400)
tk.check('resend POST without form token 400',
         tk.post(naked, '/resend-verification',
                 data={'email': 'a@b.co'}).status_code == 400)
tk.limiter.reset()

# --- 10. Settings password change --------------------------------------------
resp = tk.post(c3, '/api/settings/password',
               json={'current': 'wrong-current', 'new': 'whatever-123'})
tk.check('wrong current password rejected', resp.status_code == 400)
resp = tk.post(c3, '/api/settings/password',
               json={'current': 'brand-new-pw-99', 'new': 'short'})
tk.check('too-short new password rejected', resp.status_code == 400)
resp = tk.post(c3, '/api/settings/password',
               json={'current': 'brand-new-pw-99', 'new': 'changed-again-77'})
tk.check('password change succeeds', resp.status_code == 200)
tk.limiter.reset()
tk.check('changed password signs in',
         try_login(tk.client(), 'alice@example.test', 'changed-again-77')
         .status_code == 302)
resp = tk.post(ADMIN, '/api/settings/password',
               json={'current': 'x', 'new': 'whatever-123'})
tk.check('google account cannot set a password here', resp.status_code == 400)
tk.check('settings shows the change-password card to native accounts',
         b'Change password' in tk.get(c3, '/settings').data)
tk.check('settings hides the change-password card from google accounts',
         b'Change password' not in tk.get(ADMIN, '/settings').data)

# --- 11. Nothing sensitive leaks ---------------------------------------------
payload = tk.get(ADMIN, '/api/admin/users').get_json()
all_keys = set().union(*(set(u.keys()) for u in payload['users']))
tk.check('admin payload has no hash/token/provider fields',
         not (all_keys & {'password_hash', 'verify_token_hash',
                          'reset_token_hash', 'auth_provider',
                          'pending_invite_token', 'failed_logins'}))
by_email = {u['email']: u for u in payload['users']}
tk.check('admin payload marks unverified native signups',
         by_email['carol@example.test']['unverified'] is True
         and by_email['alice@example.test']['unverified'] is False)
rows = list(tk.FIXTURES.users.items.values())
raw_links = [tk.extract_link(m[2]) for m in M.sent if tk.extract_link(m[2])]
raw_tokens = {p.rsplit('/', 1)[-1] for p in raw_links}
tk.check('no raw emailed token is ever stored on a row',
         not any(t in str(row) for row in rows for t in raw_tokens))

# --- 12. Races that must not corrupt state -----------------------------------
# (a) Same-email signup race: the id is email-derived and the put conditional,
# so the loser gets None instead of a second row.
first = db.create_native_user(native_auth.new_user_id('dup@example.test'),
                              'dup@example.test', 'Dup', 'ph', 'th', 9999999999)
second = db.create_native_user(native_auth.new_user_id('dup@example.test'),
                               'dup@example.test', 'Dup', 'ph2', 'th2', 9999999999)
tk.check('conditional create refuses a second row for the same email',
         first is not None and second is None)
db.delete_user(first['user_id'])

# (b) A write racing account deletion must never resurrect a row: every
# native-account update is conditional on the row still existing.
rows_before = len(tk.FIXTURES.users.items)
db.set_verify_token('nat-ghost', 'h', 123)
db.set_reset_token('nat-ghost', 'h', 123)
db.set_password_hash('nat-ghost', 'h')
db.record_login_failure('nat-ghost', 10, 900)
db.clear_login_failures('nat-ghost')
tk.check('updates on a deleted row are dropped, never upserted',
         len(tk.FIXTURES.users.items) == rows_before)

# (c) The stale-purge delete is conditional on the exact observed state: a
# refreshed token (resend won the race) or a rejection keeps the row.
ghost = db.create_native_user(native_auth.new_user_id('race@example.test'),
                              'race@example.test', '', 'ph', 'th', 1)
gid = ghost['user_id']
tk.check('purge loses to a refreshed verify token',
         db.delete_stale_native_signup(gid, 999) is False
         and db.get_user(gid) is not None)
tk.FIXTURES.users.items[(gid,)]['status'] = 'rejected'
tk.check('purge loses to a rejection',
         db.delete_stale_native_signup(gid, 1) is False
         and db.get_user(gid) is not None)
tk.FIXTURES.users.items[(gid,)]['status'] = 'pending'
tk.check('purge wins on the exact observed stale state',
         db.delete_stale_native_signup(gid, 1) is True
         and db.get_user(gid) is None)

# --- 13. Stale-row purge: capacity + rejection interplay ---------------------
# A stale pending row is purged BEFORE the capacity gate, so it can never
# squat a MAX_USERS slot even when the instance is full.
tk.limiter.reset()
tk.native_signup(tk.client(), 'erin@example.test', 'password-ern-22')
erin = db.find_user_by_email('erin@example.test')
tk.FIXTURES.users.items[(erin['user_id'],)]['verify_expires_at'] = 1
saved_max = config.MAX_USERS
config.MAX_USERS = db.count_users()  # full, counting erin's stale row
tk.limiter.reset()
resp = tk.native_signup(tk.client(), 'frank@example.test', 'password-frk-33')
tk.check('stale pending row frees its slot even at capacity',
         b'Check your email' in resp.data
         and db.find_user_by_email('erin@example.test') is None
         and db.find_user_by_email('frank@example.test') is not None)
config.MAX_USERS = saved_max

# A REJECTED unverified row is a ban, not a stale signup: never purged.
frank = db.find_user_by_email('frank@example.test')
tk.post(ADMIN, f"/api/admin/users/{frank['user_id']}/reject")
tk.FIXTURES.users.items[(frank['user_id'],)]['verify_expires_at'] = 1
rows_before = len(tk.FIXTURES.users.items)
tk.limiter.reset()
resp = tk.native_signup(tk.client(), 'frank@example.test', 'password-frk-44')
frank_after = db.find_user_by_email('frank@example.test')
tk.check('rejected unverified row survives re-signup (ban sticks)',
         b'Check your email' in resp.data
         and len(tk.FIXTURES.users.items) == rows_before
         and frank_after['status'] == 'rejected'
         and frank_after['created_at'] == frank['created_at'])

# --- 14. Coexisting Google + native accounts on one email -------------------
# /forgot must reset the NATIVE account, not mail the Google notice, when
# both rows share the address.
tk.limiter.reset()
tk.sign_in(tk.client(), sub='sub-google-alice', email='alice@example.test',
           name='GAlice')
mails_before = len(M.sent)
tk.limiter.reset()
tk.post(c3, '/forgot', data={'form_token': tk.form_token(c3),
                             'email': 'alice@example.test'})
tk.check('forgot prefers the native row when a google account shares the email',
         len(M.sent) == mails_before + 1 and 'Reset' in M.sent[-1][1])

# --- 15. Pending native user signing in through an invite link ---------------
# Google-flow parity: a verified-but-pending user entering via an invite is
# approved at password sign-in (conditionally — a rejection always wins).
tk.limiter.reset()
c7 = tk.client()
tk.native_signup(c7, 'dana@example.test', 'password-dna-11')
tk.post(c7, tk.extract_link(M.sent[-1][2]))  # verify -> pending session
resp = tk.post(c3, '/api/invites', json={})
tok_dana = resp.get_json()['token']
tk.limiter.reset()
c8 = tk.client()
resp = tk.post(c8, '/login/password',
               data={'form_token': tk.form_token(c8), 'next': '/log',
                     'invite': tok_dana, 'email': 'dana@example.test',
                     'password': 'password-dna-11'})
dana = db.find_user_by_email('dana@example.test')
tk.check('pending user + invite approved at password sign-in',
         resp.status_code == 302 and resp.headers['Location'].endswith('/log')
         and dana['status'] == 'approved'
         and dana.get('invited_by') == alice['user_id']
         and tk.FIXTURES.invites.items[(tok_dana,)].get('used_by') == dana['user_id'])

# --- 15b. Password change invalidates outstanding reset links ----------------
tk.limiter.reset()
tk.post(c3, '/forgot', data={'form_token': tk.form_token(c3),
                             'email': 'alice@example.test'})
stale_reset = tk.extract_link(M.sent[-1][2])
resp = tk.post(c3, '/api/settings/password',
               json={'current': 'changed-again-77', 'new': 'yet-another-88'})
tk.check('password change succeeds with a reset link outstanding',
         resp.status_code == 200)
tk.check('outstanding reset link dead after an authenticated password change',
         tk.post(tk.client(), stale_reset,
                 data={'password': 'x' * 12, 'password2': 'x' * 12}).status_code == 404)

# --- 15c. Login-page invite banner only for currently valid invites ----------
tk.limiter.reset()
resp = tk.post(c3, '/api/invites', json={})
live_tok = resp.get_json()['token']
body_valid = tk.get(tk.client(), f'/login?invite={live_tok}').data
body_dead = tk.get(tk.client(), '/login?invite=deadbeefdeadbeef').data
tk.check('login invite banner shown only for a valid invite',
         b'approved right away' in body_valid
         and b'approved right away' not in body_dead
         and b'/login/password' in body_dead)  # page itself still renders

# --- 16. Rate limiting -------------------------------------------------------
tk.limiter.reset()
anon = tk.client()
last = None
for _ in range(6):
    last = tk.post(anon, '/signup', data={'email': 'x@y.zz', 'password': 'p' * 10})
tk.check('6th signup POST in a minute is a JSON 429',
         last.status_code == 429 and 'error' in (last.get_json() or {}))

tk.finish('M12 native accounts')
