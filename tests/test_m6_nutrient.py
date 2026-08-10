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

# --- Validation matrix -------------------------------------------------------
bad = [
    ('bad preset', {'preset': 'protein'}),
    ('missing preset', {}),
    ('empty label', {'preset': 'custom', 'label': '', 'unit': 'mg',
                     'goal': 18, 'direction': 'at_most'}),
    ('overlong label', {'preset': 'custom', 'label': 'x' * 41, 'unit': 'mg',
                        'goal': 18, 'direction': 'at_most'}),
    ('overlong unit', {'preset': 'custom', 'label': 'Iron', 'unit': 'milligrammes',
                       'goal': 18, 'direction': 'at_most'}),
    ('control chars in label', {'preset': 'custom', 'label': 'Ir\x00on', 'unit': 'mg',
                                'goal': 18, 'direction': 'at_most'}),
    ('bad direction', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                       'goal': 18, 'direction': 'sideways'}),
    ('goal zero', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                   'goal': 0, 'direction': 'at_most'}),
    ('goal negative', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                       'goal': -5, 'direction': 'at_most'}),
    ('goal NaN', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                  'goal': 'NaN', 'direction': 'at_most'}),
    ('goal Infinity', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                       'goal': 'Infinity', 'direction': 'at_most'}),
    ('goal not a number', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                           'goal': 'lots', 'direction': 'at_most'}),
    ('goal implausibly large', {'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
                                'goal': 1e9, 'direction': 'at_most'}),
    ('label with no ascii letters/digits', {'preset': 'custom', 'label': '鉄分',
                                            'unit': '㎎', 'goal': 18,
                                            'direction': 'at_most'}),
    ('custom colliding with fiber preset', {'preset': 'custom', 'label': 'Fiber',
                                            'unit': 'g', 'goal': 30,
                                            'direction': 'at_least'}),
]
for name, payload in bad:
    tk.check(f'rejected: {name}',
             tk.post(alice, '/api/settings/nutrient', json=payload).status_code == 400)

row = db.get_user('sub-alice')
tk.check('failed attempts left the row unconfigured', 'nutrient_key' not in row)

# --- Custom micro: Iron, mg, 18, at_most -------------------------------------
resp = tk.post(alice, '/api/settings/nutrient', json={
    'preset': 'custom', 'label': 'Iron', 'unit': 'mg',
    'goal': 18, 'direction': 'at_most'})
tk.check('custom micro accepted', resp.status_code == 200)
returned = (resp.get_json() or {}).get('nutrient') or {}
tk.check('response echoes the resolved config',
         returned.get('key') == 'iron_mg' and returned.get('goal') == 18 and
         returned.get('direction') == 'at_most' and returned.get('is_default') is False)

row = db.get_user('sub-alice')
tk.check('row carries the five nutrient attrs',
         row.get('nutrient_key') == 'iron_mg' and row.get('nutrient_label') == 'Iron'
         and row.get('nutrient_unit') == 'mg' and float(row.get('nutrient_goal')) == 18.0
         and row.get('nutrient_direction') == 'at_most')
cfg = config.resolve_nutrient(row)
tk.check('resolver returns the custom config with int goal',
         cfg['key'] == 'iron_mg' and cfg['goal'] == 18 and
         isinstance(cfg['goal'], int) and cfg['is_default'] is False)

# Meals store under the derived key; the old fiber field is ignored.
resp = tk.post(alice, '/api/meals', data={
    'description': 'Beef and spinach stew', 'date': DAY, 'time': '13:00',
    'iron_mg': '6.5'}, content_type='multipart/form-data')
tk.check('meal stored under custom key', resp.status_code == 201 and
         resp.get_json()['nutrients'] == {'iron_mg': 6.5})
resp = tk.post(alice, '/api/meals', data={
    'description': 'Oats', 'date': DAY, 'time': '08:00',
    'fiber_g': '3.5'}, content_type='multipart/form-data')
tk.check('stale fiber field ignored for a custom-micro user',
         resp.status_code == 201 and resp.get_json()['nutrients'] == {})
resp = tk.post(alice, '/api/meals', data={
    'description': 'Bad amount', 'date': DAY, 'iron_mg': 'much'},
    content_type='multipart/form-data')
tk.check('custom amount validated like fiber was',
         resp.status_code == 400 and 'iron_mg' in resp.get_json()['error'])

# --- Template gating ---------------------------------------------------------
resp = tk.get(alice, '/log')
tk.check('custom /log hides the fiber guide',
         b'id="guideSearch"' not in resp.data and b'Search fiber guide' not in resp.data)
tk.check('custom /log shows the label and unit',
         b'Iron' in resp.data and b'mg Iron today' in resp.data)
resp = tk.get(alice, '/review')
tk.check('custom /review shows the label', b'Iron' in resp.data)
tk.check('custom /review is in limit mode',
         b'"direction": "at_most"' in resp.data or b'at_most' in resp.data)
tk.check('custom /review drops the fiber subtitle',
         b'viscous soluble fiber' not in resp.data)
resp = tk.get(alice, '/settings')
tk.check('settings shows the current custom config',
         resp.status_code == 200 and b'Iron' in resp.data)

# --- Switch back to the fiber preset -----------------------------------------
resp = tk.post(alice, '/api/settings/nutrient', json={'preset': 'fiber'})
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
resp = tk.post(carol, '/api/settings/nutrient', json={'preset': 'fiber'})
tk.check('pending user blocked from nutrient settings', resp.status_code in (302, 403))
resp = tk.post(tk.client(), '/api/settings/nutrient', json={'preset': 'fiber'})
tk.check('anonymous blocked from nutrient settings', resp.status_code in (302, 401))

tk.finish('M6 per-user nutrient')
