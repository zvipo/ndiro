"""M7 exit test (stubbed AWS + Google): invite links — creation/validation/
listing, the public /i/ landing, auto-approve redemption for new and pending
users, single-use + race semantics, byte-identical dead-state 404s, caps,
rate limits, and account-deletion cleanup.

Run:  python tests/test_m7_invites.py
"""
import time

import testkit as tk

import config
import db

# --- Setup -------------------------------------------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
tk.post(admin, '/api/admin/users/sub-alice/approve')

# --- Page + creation ---------------------------------------------------------
tk.check('/invites renders for approved user', tk.get(alice, '/invites').status_code == 200)
tk.check('/invites blocked for anonymous', tk.get(tk.client(), '/invites').status_code == 302)

resp = tk.post(alice, '/api/invites', json={'label': 'Tino'})
tk.check('invite created with defaults (201)', resp.status_code == 201)
inv = resp.get_json()
token = inv['token']
tk.check('invite token is long (192-bit urlsafe)', len(token) >= 32)
tk.check('default expiry ~7 days', abs(inv['expires_at'] - (time.time() + 7 * 86400)) < 60)
tk.check('1-day expiry accepted',
         tk.post(alice, '/api/invites', json={'expires': '1'}).status_code == 201)
tk.check('bad expiry rejected',
         tk.post(alice, '/api/invites', json={'expires': '90'}).status_code == 400
         and tk.post(alice, '/api/invites', json={'expires': 'never'}).status_code == 400)
tk.check('long label rejected',
         tk.post(alice, '/api/invites', json={'label': 'x' * 101}).status_code == 400)

resp = tk.get(alice, '/api/invites')
tk.check('own invites listed', resp.status_code == 200 and len(resp.get_json()['invites']) == 2)
tk.check('listing never exposes used_by',
         all('used_by' not in i for i in resp.get_json()['invites']))

# --- Public landing ----------------------------------------------------------
anon = tk.client()
resp = tk.get(anon, f'/i/{token}')
tk.check('valid invite page renders anonymously',
         resp.status_code == 200 and b'Alice' in resp.data and
         f'/login?invite={token}'.encode() in resp.data)
tk.check('invite page never leaks the inviter email',
         b'alice@example.test' not in resp.data)
tk.check('corner-menu sign-in also carries the invite (no bare next=/i/ link)',
         b'next=/i/' not in resp.data)

# --- New-user redemption ------------------------------------------------------
friend = tk.client()
resp = tk.sign_in(friend, sub='sub-friend', email='friend@example.test', name='Friend',
                  login_url=f'/login?invite={token}&next=/log')
row = db.get_user('sub-friend')
tk.check('invited signup lands approved on /log',
         resp.status_code == 302 and resp.headers['Location'].endswith('/log') and
         row['status'] == 'approved' and 'approved_at' in row)
tk.check('invited_by recorded', row.get('invited_by') == 'sub-alice')
with tk.session(friend) as sess:
    tk.check('post-login session carries no invite token',
             set(sess.keys()) <= {'user_id', '_permanent'})
invite_row = db.get_invite(token)
tk.check('invite consumed (used_by/used_at set)',
         invite_row.get('used_by') == 'sub-friend' and 'used_at' in invite_row)
used_page = tk.get(anon, f'/i/{token}')
tk.check('used invite page is 404', used_page.status_code == 404)
tk.check('revoking an already-used invite is 404 (nothing was stopped)',
         tk.delete(alice, f'/api/invites/{token}').status_code == 404 and
         not db.get_invite(token).get('revoked'))

# Same token again: next signup falls back to pending.
second = tk.client()
resp = tk.sign_in(second, sub='sub-second', email='second@example.test', name='Second',
                  login_url=f'/login?invite={token}&next=/log')
tk.check('second use of a single-use invite falls back to pending',
         resp.headers['Location'].endswith('/waiting') and
         db.get_user('sub-second')['status'] == 'pending')

# --- Existing pending user redeems -------------------------------------------
tok2 = tk.post(alice, '/api/invites', json={}).get_json()['token']
resp = tk.sign_in(second, sub='sub-second', email='second@example.test', name='Second',
                  login_url=f'/login?invite={tok2}&next=/log')
row = db.get_user('sub-second')
tk.check('pending user redeeming an invite becomes approved',
         resp.headers['Location'].endswith('/log') and row['status'] == 'approved'
         and row.get('invited_by') == 'sub-alice')
tk.check('pending redemption consumed the invite',
         db.get_invite(tok2).get('used_by') == 'sub-second')

# --- Existing approved user: no-op, not consumed ------------------------------
tok3 = tk.post(alice, '/api/invites', json={}).get_json()['token']
tk.sign_in(friend, sub='sub-friend', email='friend@example.test', name='Friend',
           login_url=f'/login?invite={tok3}&next=/log')
tk.check('approved user re-signing with an invite does not consume it',
         'used_by' not in db.get_invite(tok3) and
         tk.get(anon, f'/i/{tok3}').status_code == 200)

# --- Rejected user stays rejected, invite unconsumed --------------------------
tk.post(admin, '/api/admin/users/sub-second/reject')
resp = tk.sign_in(second, sub='sub-second', email='second@example.test', name='Second',
                  login_url=f'/login?invite={tok3}&next=/log')
row = db.get_user('sub-second')
tk.check('rejected user cannot redeem an invite',
         row['status'] == 'rejected' and 'used_by' not in db.get_invite(tok3) and
         resp.headers['Location'].endswith('/'))
tk.check('conditional approval refuses a non-pending user (rejection sticks)',
         db.approve_pending_user('sub-second', 'sub-alice') is False and
         db.get_user('sub-second')['status'] == 'rejected')

# --- Claim condition is self-sufficient (revoked/expired refused in-write) ----
tk.limiter.reset()
tok_dead = tk.post(alice, '/api/invites', json={}).get_json()['token']
tk.delete(alice, f'/api/invites/{tok_dead}')
tk.check('claim refuses a revoked invite even without the pre-read',
         db.claim_invite(tok_dead, 'sub-x') is False)
tok_exp = tk.post(alice, '/api/invites', json={}).get_json()['token']
tk.FIXTURES.invites.items[(tok_exp,)]['expires_at'] = int(time.time()) - 5
tk.check('claim refuses an expired invite in-condition',
         db.claim_invite(tok_exp, 'sub-x') is False)

# --- Crash-retry: a failed account write does not burn the invite -------------
tok_crash = tk.post(alice, '/api/invites', json={}).get_json()['token']
_real_create_user = db.create_user
def _boom(*a, **k):
    raise RuntimeError('injected account-write failure')
db.create_user = _boom
crashy = tk.client()
resp = tk.sign_in(crashy, sub='sub-crash', email='crash@example.test', name='Crash',
                  login_url=f'/login?invite={tok_crash}&next=/log')
db.create_user = _real_create_user
tk.check('crashed redemption: 500, no row, claim held by the claimant',
         resp.status_code == 500 and db.get_user('sub-crash') is None and
         db.get_invite(tok_crash).get('used_by') == 'sub-crash')
resp = tk.sign_in(crashy, sub='sub-crash', email='crash@example.test', name='Crash',
                  login_url=f'/login?invite={tok_crash}&next=/log')
tk.check('retry through the same link completes the approved signup',
         resp.status_code == 302 and resp.headers['Location'].endswith('/log') and
         db.get_user('sub-crash')['status'] == 'approved' and
         db.get_user('sub-crash').get('invited_by') == 'sub-alice')
tk.check('a DIFFERENT user still cannot use the crash-claimed invite',
         db.claim_invite(tok_crash, 'sub-other') is False)

# --- Dead inviter: links die with the inviter's status ------------------------
tk.post(admin, '/api/admin/users/sub-alice/reject')
tk.check('rejected inviter kills the /i/ page', tk.get(anon, f'/i/{tok3}').status_code == 404)
newbie = tk.client()
resp = tk.sign_in(newbie, sub='sub-newbie', email='newbie@example.test', name='Newbie',
                  login_url=f'/login?invite={tok3}&next=/log')
tk.check('rejected inviter cannot mint approved accounts',
         db.get_user('sub-newbie')['status'] == 'pending' and
         'used_by' not in db.get_invite(tok3))
tk.post(admin, '/api/admin/users/sub-alice/approve')
tk.post(admin, '/api/admin/users/sub-newbie/approve')

# --- MAX_USERS full: invite NOT consumed --------------------------------------
config.MAX_USERS = db.count_users()
overflow = tk.client()
resp = tk.sign_in(overflow, sub='sub-overflow', email='overflow@example.test',
                  name='Overflow', login_url=f'/login?invite={tok3}&next=/log')
tk.check('full instance: 403, no row, invite unconsumed',
         resp.status_code == 403 and db.get_user('sub-overflow') is None and
         'used_by' not in db.get_invite(tok3))
config.MAX_USERS = 100

# --- Byte-identical dead-state QUADRUPLE --------------------------------------
tk.limiter.reset()
missing_page = tk.get(anon, '/i/no-such-token-aaaaaaaaaaaaaaaaaaa')
tok_revoked = tk.post(alice, '/api/invites', json={}).get_json()['token']
tk.delete(alice, f'/api/invites/{tok_revoked}')
revoked_page = tk.get(anon, f'/i/{tok_revoked}')
tok_expired = tk.post(alice, '/api/invites', json={}).get_json()['token']
tk.FIXTURES.invites.items[(tok_expired,)]['expires_at'] = int(time.time()) - 10
expired_page = tk.get(anon, f'/i/{tok_expired}')
tk.check('all four dead states are 404',
         missing_page.status_code == revoked_page.status_code ==
         expired_page.status_code == used_page.status_code == 404)
tk.check('missing/revoked/expired/used pages byte-identical',
         missing_page.data == revoked_page.data == expired_page.data == used_page.data)

# --- Ownership + caps ---------------------------------------------------------
tk.limiter.reset()
tok4 = tk.post(alice, '/api/invites', json={}).get_json()['token']
friend_c = friend  # approved non-owner
tk.check("foreign revoke is 404, invite stays live",
         tk.delete(friend_c, f'/api/invites/{tok4}').status_code == 404 and
         tk.get(anon, f'/i/{tok4}').status_code == 200)

config.MAX_ACTIVE_INVITES = 2
# alice currently has some active invites; revoke all to reset, then fill the cap.
for i in tk.get(alice, '/api/invites').get_json()['invites']:
    if i['active']:
        tk.delete(alice, f"/api/invites/{i['token']}")
tk.post(alice, '/api/invites', json={})
tk.post(alice, '/api/invites', json={})
resp = tk.post(alice, '/api/invites', json={})
tk.check('active-invite cap enforced (dead links do not count)',
         resp.status_code == 400 and 'Limit' in resp.get_json()['error'])
config.MAX_ACTIVE_INVITES = 10

# --- Junk invite params are harmless ------------------------------------------
junk = tk.client()
resp = tk.sign_in(junk, sub='sub-junk', email='junk@example.test', name='Junk',
                  login_url='/login?invite=../../evil&next=/log')
with tk.session(junk) as sess:
    tk.check('junk invite param: normal pending flow, clean session',
             db.get_user('sub-junk')['status'] == 'pending' and
             set(sess.keys()) <= {'user_id', '_permanent'})

# --- Admin attribution ---------------------------------------------------------
resp = tk.get(admin, '/api/admin/users')
by_id = {u['user_id']: u for u in resp.get_json()['users']}
tk.check('admin payload shows inviter id + email',
         by_id['sub-friend']['invited_by'] == 'sub-alice' and
         by_id['sub-friend']['invited_by_email'] == 'alice@example.test')

# --- Account deletion wipes invites -------------------------------------------
tk.limiter.reset()
live_tok = tk.post(alice, '/api/invites', json={}).get_json()['token']
tk.post(alice, '/api/account/delete', json={'confirm': 'delete'})
tk.check('deletion removed invite rows', db.list_user_invites('sub-alice') == [])
tk.check('live invite dead after inviter deletion',
         tk.get(anon, f'/i/{live_tok}').status_code == 404)

# --- Rate limit on the public landing -----------------------------------------
tk.limiter.reset()
statuses = [tk.get(anon, '/i/rate-limit-probe').status_code for _ in range(40)]
tk.check('invite landing rate limit kicks in', 429 in statuses)

tk.finish('M7 invite links')
