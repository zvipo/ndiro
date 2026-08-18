"""M10 exit test: the admin monitoring page and /api/admin/stats.

Covers: admin-only access, the counts themselves (accounts, meals, photos,
shares, invites, AI use), the client-anchored activity windows, orphan
detection, per-section degradation when a backend fails, and — the review
point — that a monitoring surface reports COUNTS ONLY: no meal description,
context, or nutrient value can appear in the payload, because the scan behind
it never loads them.

Run:  python tests/test_m10_monitor.py
"""
import io
import time

import testkit as tk

import config
import db

# --- Setup: an admin, two approved users, one pending ------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
bob = tk.client()
tk.sign_in(bob, sub='sub-bob', email='bob@example.test', name='Bob')
pat = tk.client()
tk.sign_in(pat, sub='sub-pat', email='pat@example.test', name='Pat')  # stays pending
tk.post(admin, '/api/admin/users/sub-alice/approve')
tk.post(admin, '/api/admin/users/sub-bob/approve')

TODAY = '2026-08-18'          # the anchor every assertion below is measured from
RECENT = '2026-08-14'         # inside the 7-day window
OLD = '2026-06-01'            # outside the 30-day window
SECRET_DESC = 'Two bowls of black bean chili'  # must never reach the payload

# Alice: 3 meals on 2 days, one with a photo. Bob: 1 old meal.
for day, desc, time_str in ((TODAY, SECRET_DESC, '08:15'),
                            (TODAY, 'Lentil soup', '12:30'),
                            (RECENT, 'Oats', '07:00')):
    resp = tk.post(alice, '/api/meals', data={'description': desc, 'date': day,
                                              'time': time_str, 'fiber_g': '3.5'})
    assert resp.status_code == 201, resp.get_json()
resp = tk.post(alice, '/api/meals', data={
    'description': 'Barley salad', 'date': TODAY, 'time': '19:00', 'fiber_g': '2',
    'photo': (io.BytesIO(tk.TINY_JPEG), 'p.jpg')}, content_type='multipart/form-data')
assert resp.status_code == 201, resp.get_json()
resp = tk.post(bob, '/api/meals', data={'description': 'Toast', 'date': OLD,
                                        'time': '09:00', 'fiber_g': '1'})
assert resp.status_code == 201, resp.get_json()

# Links: one live share, one revoked; one open invite.
live_share = tk.post(alice, '/api/shares', json={'label': 'dietician'}).get_json()['token']
dead_share = tk.post(alice, '/api/shares', json={}).get_json()['token']
tk.delete(alice, f'/api/shares/{dead_share}')
tk.post(bob, '/api/invites', json={'expires': '7'})


PHOTO_BYTES = len(next(v for k, v in tk.FIXTURES.s3.objects.items()
                       if k.startswith('users/sub-alice/')))


def stats(client=admin, anchor=TODAY):
    # Reset first: the route is rate limited at 12/min and a test file makes
    # far more calls than a human would.
    tk.limiter.reset()
    return tk.get(client, f'/api/admin/stats?anchor={anchor}')


# --- 1. Admin-only, like every other admin surface ---------------------------
tk.check('admin opens the monitor page', tk.get(admin, '/admin/monitor').status_code == 200)
tk.check('admin stats API works', stats().status_code == 200)
tk.check('approved non-admin blocked from the page',
         tk.get(alice, '/admin/monitor').status_code == 302)
tk.check('approved non-admin blocked from the stats API',
         tk.get(alice, '/api/admin/stats').status_code == 403)
tk.check('pending user blocked from the stats API',
         tk.get(pat, '/api/admin/stats').status_code == 403)
tk.check('signed-out visitor blocked from the stats API',
         tk.get(tk.client(), '/api/admin/stats').status_code == 401)

# --- 2. The headline counts --------------------------------------------------
d = stats().get_json()
acc, meals, photos = d['accounts'], d['meals'], d['photos']
tk.check('accounts counted', acc['total'] == 4 and acc['max'] == config.MAX_USERS)
tk.check('accounts broken down by status',
         acc['by_status'] == {'admin': 1, 'approved': 2, 'pending': 1})
tk.check('meals counted across all users', meals['total'] == 5)
tk.check('distinct user-days counted', meals['days_logged'] == 3)
tk.check('accounts that have logged counted', meals['logging_accounts'] == 2)
tk.check('photos counted', photos['enabled'] and photos['total'] == 1)
tk.check('photo bytes counted', photos['bytes'] == PHOTO_BYTES)
tk.check('shares bucketed', d['shares'] == {'total': 2, 'active': 1, 'revoked': 1,
                                            'expired': 0, 'truncated': False})
tk.check('invites bucketed', d['invites']['total'] == 1 and d['invites']['open'] == 1
         and d['invites']['used'] == 0)
tk.check('nothing is orphaned in a healthy instance',
         d['orphans'] == {'meals': 0, 'photos': 0})
tk.check('instance facts carried', d['instance']['ai_daily_limit'] == config.AI_DAILY_LIMIT
         and d['instance']['photos_enabled'] is True)

# --- 3. COUNTS ONLY: no meal content can reach an admin (invariant #7) -------
body = stats().get_data(as_text=True)
tk.check('stats payload carries no meal description', SECRET_DESC not in body)
tk.check('stats payload carries no other meal descriptions',
         not any(w in body for w in ('Lentil soup', 'Barley salad', 'Toast', 'Oats')))
tk.check('stats payload carries no nutrient values', '"fiber_g"' not in body)
tk.check('monitor page ships no meal data of its own',
         SECRET_DESC not in tk.get(admin, '/admin/monitor').get_data(as_text=True))
# The guarantee is structural: the scan itself never loads the columns.
scanned = db.scan_meal_stats()
tk.check('the meals scan projects away everything but user_id and date',
         set(scanned['per_user']) == {'sub-alice', 'sub-bob'}
         and scanned['total'] == 5)
raw = db.meals_table().scan(ProjectionExpression='user_id, #d',
                            ExpressionAttributeNames={'#d': 'date'})['Items']
tk.check('projected rows contain no description/nutrients attribute',
         all(set(i) <= {'user_id', 'date'} for i in raw))
tk.check('share tokens never appear in the stats payload',
         live_share not in body and dead_share not in body)

# --- 4. Activity windows use the CLIENT's anchor, not the server clock -------
tk.check('7-day window counts only recent meals', meals['logged_7d'] == 4)
tk.check('30-day window excludes the old meal', meals['logged_30d'] == 4)
tk.check('active accounts in 7 days', meals['active_7d'] == 1)
tk.check('active accounts in 30 days', meals['active_30d'] == 1)
far = stats(anchor='2026-09-30').get_json()['meals']
tk.check('a later anchor moves the window', far['logged_7d'] == 0 and far['logged_30d'] == 0)
old_anchor = stats(anchor='2026-06-02').get_json()['meals']
tk.check('an earlier anchor catches the old meal', old_anchor['logged_7d'] == 1)
tk.check('a junk anchor falls back to UTC today rather than 400ing',
         stats(anchor='not-a-date').status_code == 200)
# A date within a month of date.min/date.max overflows the window arithmetic —
# _valid_date rejects it so no caller has to (the meals API 400s on the same).
tk.check('date.min/date.max anchors are rejected, not 500s',
         tk.app_module._valid_date('0001-01-01') is None
         and tk.app_module._valid_date('9999-12-31') is None
         and tk.app_module._valid_date('2026-08-18') == '2026-08-18')
tk.check('an unrepresentable anchor degrades to UTC today',
         stats(anchor='0001-01-01').status_code == 200
         and stats(anchor='9999-12-31').status_code == 200)

# --- 5. NOTHING on this page is attributable to one account ------------------
# /privacy promises admins see account metadata and nothing more. The dashboard
# reports instance totals, so no per-account row, id, email, or date may appear
# — even though db.py groups by user_id internally to compute the totals.
d = stats().get_json()
body = stats().get_data(as_text=True)
tk.check('payload carries no per-account rows', 'users' not in d)
tk.check('payload carries no user ids',
         not any(uid in body for uid in ('sub-alice', 'sub-bob', 'sub-admin', 'sub-pat')))
tk.check('payload carries no email addresses', '@example.test' not in body)
# The page itself only ever names the admin viewing it (base.html's menu shows
# your own email on every page) — never another account.
page = tk.get(admin, '/admin/monitor').get_data(as_text=True)
tk.check('the rendered page names no account but the viewer',
         not any(e in page for e in ('alice@example.test', 'bob@example.test',
                                     'pat@example.test'))
         and 'admin@example.test' in page)
tk.check('share stats are totals, not a per-user breakdown', 'per_user' not in d['shares'])
tk.check('invite stats are totals, not a per-user breakdown', 'per_user' not in d['invites'])
tk.check('photo stats are totals, not a per-user breakdown', 'per_user' not in d['photos'])
tk.check('no meal dates leak as per-account first/last',
         not any(k in body for k in ('first_logged', 'last_logged')))
# The internal grouping still has to exist — it is what the cardinalities and
# the orphan check are computed from. It just must not be serialized.
tk.check('db.py still groups internally so totals can be counted',
         set(db.scan_meal_stats()['per_user']) == {'sub-alice', 'sub-bob'})
tk.check('active-account counts survive without exposing who',
         d['meals']['active_7d'] == 1 and d['meals']['logging_accounts'] == 2)

# --- 6. AI use is reported for the UTC day the cap actually uses -------------
db.users_table().update_item(
    Key={'user_id': 'sub-alice'},
    UpdateExpression='SET ai_uses_date = :d, ai_uses_today = :n',
    ExpressionAttributeValues={':d': tk.app_module._utc_today_str(), ':n': config.AI_DAILY_LIMIT})
db.users_table().update_item(
    Key={'user_id': 'sub-bob'},
    UpdateExpression='SET ai_uses_date = :d, ai_uses_today = :n',
    ExpressionAttributeValues={':d': '2000-01-01', ':n': 99})  # a stale day must not count
acc = stats().get_json()['accounts']
tk.check('AI uses today summed', acc['ai_uses_today'] == config.AI_DAILY_LIMIT)
tk.check('users at the daily cap counted', acc['ai_at_cap'] == 1)
tk.check('AI users today counted', acc['ai_users_today'] == 1)
# Bob's 99 uses are stamped 2000-01-01; only the current UTC day may be summed.
tk.check("a stale ai_uses_date does not leak into today's total",
         acc['ai_uses_today'] == config.AI_DAILY_LIMIT and acc['ai_users_today'] == 1)

# --- 7. Orphans: data left behind by a half-finished account deletion --------
db.users_table().delete_item(Key={'user_id': 'sub-bob'})
d = stats().get_json()
tk.check('orphaned meal rows surfaced', d['orphans']['meals'] == 1)
tk.check('the orphan count names no one',
         set(d['orphans']) == {'meals', 'photos'}
         and all(isinstance(v, int) for v in d['orphans'].values()))
tk.check('a deleted account drops out of the totals',
         d['accounts']['total'] == 3 and d['meals']['logging_accounts'] == 2)
db.create_user('sub-bob', 'bob@example.test', 'Bob', 'approved')

# --- 8. One failing backend degrades its section, never the whole page -------
saved = db.scan_photo_stats
db.scan_photo_stats = lambda: (_ for _ in ()).throw(RuntimeError('S3 down'))
resp = stats()
d = resp.get_json()
tk.check('a dead S3 still returns 200', resp.status_code == 200)
tk.check('the photo section degrades to null', d['photos'] is None)
tk.check('orphan detection degrades with it', d['orphans'] is None)
tk.check('the other sections still report', d['meals']['total'] == 5 and d['accounts']['total'] == 4)
db.scan_photo_stats = saved

saved_users = db.list_users
db.list_users = lambda: (_ for _ in ()).throw(RuntimeError('DynamoDB down'))
tk.check('an unreadable users table is a 500, not a blank page',
         stats().status_code == 500)
db.list_users = saved_users

# --- 9. Photos off is a normal state, not an error ---------------------------
saved_bucket = config.S3_BUCKET
config.S3_BUCKET = ''
d = stats().get_json()
tk.check('no bucket configured reports photos disabled',
         d['photos']['enabled'] is False and d['photos']['total'] == 0)
tk.check('instance flag matches', d['instance']['photos_enabled'] is False)
config.S3_BUCKET = saved_bucket

# --- 10. Scans are page-capped so one refresh can never run away -------------
saved_cap = db._STATS_SCAN_PAGE_CAP


class _EndlessTable:
    """A table whose scan always claims there is another page."""

    def scan(self, **kwargs):
        return {'Items': [{'user_id': 'sub-alice', 'date': TODAY}],
                'LastEvaluatedKey': {'user_id': 'sub-alice', 'sk': 'x'}}


saved_meals_table = db.meals_table
db.meals_table = lambda: _EndlessTable()
db._STATS_SCAN_PAGE_CAP = 3
capped = db.scan_meal_stats()
tk.check('the meals scan stops at the page cap', capped['total'] == 3)
tk.check('a capped scan says so instead of lying', capped['truncated'] is True)
db.meals_table = saved_meals_table
db._STATS_SCAN_PAGE_CAP = saved_cap

# --- 11. Rate limited: a full-table scan per request is cheap, not free ------
tk.limiter.reset()
codes = [tk.get(admin, f'/api/admin/stats?anchor={TODAY}').status_code for _ in range(14)]
tk.check('stats API is rate limited', 429 in codes)
tk.check('the limit is not so tight it blocks normal use', codes[:12] == [200] * 12)
tk.limiter.reset()

tk.finish('M10 admin monitor')
