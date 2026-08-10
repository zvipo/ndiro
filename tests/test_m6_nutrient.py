"""M6 exit test (stubbed AWS + Google): per-user tracked nutrient — default
fiber resolution, the /api/settings/nutrient validation matrix, custom-micro
meal logging under the derived key, template gating (guide only for fiber),
direction-aware review page, and switching back to the fiber preset.

Run:  python tests/test_m6_nutrient.py
"""
import testkit as tk

import config
import db

# --- Setup -------------------------------------------------------------------
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
alice = tk.client()
tk.sign_in(alice, sub='sub-alice', email='alice@example.test', name='Alice')
tk.post(admin, '/api/admin/users/sub-alice/approve')

DAY = '2026-08-05'

# --- Default resolution (no migration) ---------------------------------------
cfg = config.resolve_nutrient(db.get_user('sub-alice'))
tk.check('fresh user resolves to the fiber default',
         cfg == {'key': 'fiber_g', 'label': 'viscous fiber', 'unit': 'g',
                 'goal': 20, 'direction': 'at_least', 'is_default': True})
tk.check('resolver tolerates None', config.resolve_nutrient(None)['key'] == 'fiber_g')
tk.check('default goal serializes as int (no "20.0 g goal")',
         isinstance(cfg['goal'], int))

resp = tk.get(alice, '/log')
tk.check('default /log shows the fiber guide search',
         b'id="guideSearch"' in resp.data and b'Search fiber guide' in resp.data)
tk.check('default /log shows fiber stat label', b'g viscous fiber today' in resp.data)

# --- Validation matrix (closed catalog + goal override) ----------------------
bad = [
    ('unknown key', {'key': 'unobtainium_mg', 'goal': 5}),
    ('missing key', {'goal': 5}),
    ('free-form key rejected', {'key': 'constructor', 'goal': 5}),
    ('goal zero', {'key': 'sodium_mg', 'goal': 0}),
    ('goal negative', {'key': 'sodium_mg', 'goal': -5}),
    ('goal NaN', {'key': 'sodium_mg', 'goal': 'NaN'}),
    ('goal Infinity', {'key': 'sodium_mg', 'goal': 'Infinity'}),
    ('goal not a number', {'key': 'sodium_mg', 'goal': 'lots'}),
    ('goal implausibly large', {'key': 'sodium_mg', 'goal': 1e9}),
    # '' is a cleared/unparseable UI field, NOT an omission — silent reset
    # of a personalized goal to the default would be data loss.
    ('goal empty string', {'key': 'sodium_mg', 'goal': ''}),
]
for name, payload in bad:
    tk.check(f'rejected: {name}',
             tk.post(alice, '/api/settings/nutrient', json=payload).status_code == 400)

row = db.get_user('sub-alice')
tk.check('failed attempts left the row unconfigured', 'nutrient_key' not in row)

# --- Catalog micro: sodium, custom limit -------------------------------------
resp = tk.post(alice, '/api/settings/nutrient', json={
    'key': 'sodium_mg', 'goal': 2000})
tk.check('catalog micro accepted', resp.status_code == 200)
returned = (resp.get_json() or {}).get('nutrient') or {}
tk.check('response echoes the resolved config',
         returned.get('key') == 'sodium_mg' and returned.get('goal') == 2000 and
         returned.get('direction') == 'at_most' and returned.get('is_default') is False)

row = db.get_user('sub-alice')
tk.check('row carries the five nutrient attrs',
         row.get('nutrient_key') == 'sodium_mg' and row.get('nutrient_label') == 'sodium'
         and row.get('nutrient_unit') == 'mg' and float(row.get('nutrient_goal')) == 2000.0
         and row.get('nutrient_direction') == 'at_most')
cfg = config.resolve_nutrient(row)
tk.check('resolver returns the catalog config with int goal',
         cfg['key'] == 'sodium_mg' and cfg['goal'] == 2000 and
         isinstance(cfg['goal'], int) and cfg['is_default'] is False)

# Omitted goal falls back to the catalog default; direction comes from the
# catalog, never the client.
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'added_sugar_g',
                                                      'direction': 'at_least'})
tk.check('omitted goal uses the catalog default (and direction is server-side)',
         resp.status_code == 200 and
         resp.get_json()['nutrient']['goal'] == 25 and
         resp.get_json()['nutrient']['direction'] == 'at_most')
tk.check('non-personalized goal stores the follow-the-default sentinel',
         float(db.get_user('sub-alice')['nutrient_goal']) == 0.0)
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'added_sugar_g',
                                                      'goal': 25})
tk.check('goal equal to the catalog default also stores the sentinel',
         resp.status_code == 200 and
         float(db.get_user('sub-alice')['nutrient_goal']) == 0.0)

# Catalog label/unit/direction are authoritative — a tampered row snapshot
# does not override them, so catalog edits reach every user immediately.
row = dict(db.get_user('sub-alice'))
row['nutrient_label'] = 'hacked'
row['nutrient_direction'] = 'at_least'
cfg = config.resolve_nutrient(row)
tk.check('catalog wins over the row snapshot for catalog keys',
         cfg['label'] == 'added sugar' and cfg['direction'] == 'at_most')

# The fiber default keeps its guide but honors a personalized goal.
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'fiber_g', 'goal': 25})
tk.check('fiber with a personal goal stays the default experience',
         resp.status_code == 200 and
         resp.get_json()['nutrient']['is_default'] is True and
         resp.get_json()['nutrient']['goal'] == 25)
tk.check('guide still renders with a personalized fiber goal',
         b'id="guideSearch"' in tk.get(alice, '/log').data)

# A legacy free-form row (pre-catalog) still resolves — no migration needed.
db.set_user_nutrient('sub-alice', 'magnesium_mg', 'Magnesium', 'mg', 400, 'at_least')
cfg = config.resolve_nutrient(db.get_user('sub-alice'))
tk.check('legacy non-catalog row still resolves',
         cfg['key'] == 'magnesium_mg' and cfg['goal'] == 400 and
         cfg['is_default'] is False)
resp = tk.get(alice, '/settings')
tk.check('legacy row settings page keeps the current micro selectable',
         resp.status_code == 200 and b'magnesium_mg' in resp.data and
         b'Magnesium (mg)' in resp.data)
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'magnesium_mg',
                                                      'goal': 500})
tk.check('legacy owner can keep their micro and adjust its goal',
         resp.status_code == 200 and
         resp.get_json()['nutrient'] == {'key': 'magnesium_mg', 'label': 'Magnesium',
                                         'unit': 'mg', 'goal': 500,
                                         'direction': 'at_least', 'is_default': False})
tk.check('nobody can CREATE a non-catalog micro',
         tk.post(alice, '/api/settings/nutrient',
                 json={'key': 'unicorn_dust_g', 'goal': 5}).status_code == 400)

# Back to the catalog for the rest of the suite.
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'sodium_mg', 'goal': 2000})
assert resp.status_code == 200

# Meals store under the derived key; the old fiber field is ignored.
resp = tk.post(alice, '/api/meals', data={
    'description': 'Beef and spinach stew', 'date': DAY, 'time': '13:00',
    'sodium_mg': '650'}, content_type='multipart/form-data')
tk.check('meal stored under catalog key', resp.status_code == 201 and
         resp.get_json()['nutrients'] == {'sodium_mg': 650.0})
resp = tk.post(alice, '/api/meals', data={
    'description': 'Oats', 'date': DAY, 'time': '08:00',
    'fiber_g': '3.5'}, content_type='multipart/form-data')
tk.check('stale fiber field ignored for a custom-micro user',
         resp.status_code == 201 and resp.get_json()['nutrients'] == {})
resp = tk.post(alice, '/api/meals', data={
    'description': 'Bad amount', 'date': DAY, 'sodium_mg': 'much'},
    content_type='multipart/form-data')
tk.check('catalog amount validated like fiber was',
         resp.status_code == 400 and 'sodium_mg' in resp.get_json()['error'])

# A form posted from a page rendered under a DIFFERENT micro (stale tab) is
# rejected instead of silently dropping the typed amount.
resp = tk.post(alice, '/api/meals', data={
    'description': 'stale tab', 'date': DAY, 'time': '09:00',
    'fiber_g': '3.5', 'nutrient_key': 'fiber_g'},
    content_type='multipart/form-data')
tk.check('stale-tab meal post rejected with a clear error',
         resp.status_code == 400 and 'reload' in resp.get_json()['error'])

# Editing a meal logged under a previous micro keeps that meal's old-key value.
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'fiber_g'})
assert resp.status_code == 200
resp = tk.post(alice, '/api/meals', data={
    'description': 'old fiber meal', 'date': DAY, 'time': '07:00',
    'fiber_g': '5.5'}, content_type='multipart/form-data')
assert resp.status_code == 201, resp.get_json()
old_meal_id = resp.get_json()['meal_id']
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'sodium_mg', 'goal': 2000})
assert resp.status_code == 200
resp = tk.put(alice, f'/api/meals/{DAY}/{old_meal_id}', data={
    'description': 'old fiber meal, typo fixed', 'sodium_mg': ''})
tk.check('editing an old-micro meal preserves its stored value',
         resp.status_code == 200 and
         resp.get_json()['nutrients'] == {'fiber_g': 5.5})
resp = tk.put(alice, f'/api/meals/{DAY}/{old_meal_id}', data={
    'description': 'old fiber meal', 'sodium_mg': '300'})
tk.check('edit under the new micro adds its key alongside the old value',
         resp.status_code == 200 and
         resp.get_json()['nutrients'] == {'fiber_g': 5.5, 'sodium_mg': 300.0})

# Invalid goals degrade safely, never NaN-ing the chart: a catalog key falls
# back to ITS catalog default; a legacy key (no catalog entry to fall back
# on) degrades to the fiber default.
row = dict(db.get_user('sub-alice'))  # sodium_mg
row['nutrient_goal'] = 0
tk.check('catalog row with zero/sentinel goal uses the catalog default',
         config.resolve_nutrient(row)['goal'] == 2300)
row['nutrient_goal'] = float('nan')
tk.check('catalog row with NaN goal uses the catalog default',
         config.resolve_nutrient(row)['goal'] == 2300)
legacy_row = {'nutrient_key': 'magnesium_mg', 'nutrient_label': 'Magnesium',
              'nutrient_unit': 'mg', 'nutrient_goal': 0,
              'nutrient_direction': 'at_least'}
tk.check('legacy row with corrupt goal degrades to the fiber default',
         config.resolve_nutrient(legacy_row)['is_default'] is True)

# --- Template gating ---------------------------------------------------------
resp = tk.get(alice, '/log')
tk.check('custom /log hides the fiber guide',
         b'id="guideSearch"' not in resp.data and b'Search fiber guide' not in resp.data)
tk.check('custom /log shows the label and unit',
         b'Sodium (mg)' in resp.data and b'mg sodium today' in resp.data)
resp = tk.get(alice, '/review')
tk.check('custom /review shows the label', b'sodium' in resp.data)
tk.check('custom /review is in limit mode',
         b'"direction": "at_most"' in resp.data or b'at_most' in resp.data)
tk.check('custom /review drops the fiber subtitle',
         b'viscous soluble fiber' not in resp.data)
resp = tk.get(alice, '/settings')
tk.check('settings shows the current config and the catalog dropdown',
         resp.status_code == 200 and b'sodium' in resp.data and
         b'nutrientSelect' in resp.data and b'cholesterol_mg' in resp.data)

# --- Switch back to the fiber preset -----------------------------------------
resp = tk.post(alice, '/api/settings/nutrient', json={'key': 'fiber_g'})
tk.check('fiber preset accepted', resp.status_code == 200 and
         resp.get_json()['nutrient']['is_default'] is True)
cfg = config.resolve_nutrient(db.get_user('sub-alice'))
tk.check('preset restores the full default config',
         cfg == config.DEFAULT_NUTRIENT and cfg is not config.DEFAULT_NUTRIENT)
resp = tk.get(alice, '/log')
tk.check('guide is back after preset restore', b'id="guideSearch"' in resp.data)

# --- Guards ------------------------------------------------------------------
carol = tk.client()
tk.sign_in(carol, sub='sub-carol', email='carol@example.test', name='Carol')
resp = tk.post(carol, '/api/settings/nutrient', json={'key': 'fiber_g'})
tk.check('pending user blocked from nutrient settings', resp.status_code in (302, 403))
resp = tk.post(tk.client(), '/api/settings/nutrient', json={'key': 'fiber_g'})
tk.check('anonymous blocked from nutrient settings', resp.status_code in (302, 401))

tk.finish('M6 per-user nutrient')
