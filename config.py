"""Ndiro configuration: env loading, hard-fail checks, and app constants.

Everything configurable comes from the environment (.env via python-dotenv).
No secrets, bucket names, hostnames, or emails are ever hardcoded here.
"""
import math
import os
import re

from dotenv import load_dotenv

# Load ONLY the app's own .env (default find_dotenv walks up parent
# directories and could pick up an unrelated file). Real env vars still win.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# --- Flask session signing key (REQUIRED — no insecure fallback) -------------
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        'SECRET_KEY is not set. Ndiro is a multi-user app and refuses to boot '
        'with an insecure default. Generate one with:\n'
        '  python3 -c "import secrets; print(secrets.token_urlsafe(48))"\n'
        'and set it in .env (see env_template.txt).'
    )

# --- AWS / storage -----------------------------------------------------------
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')

USERS_TABLE = os.getenv('USERS_TABLE', 'ndiro-users')
MEALS_TABLE = os.getenv('MEALS_TABLE', 'ndiro-meals')
SHARES_TABLE = os.getenv('SHARES_TABLE', 'ndiro-shares')
INVITES_TABLE = os.getenv('INVITES_TABLE', 'ndiro-invites')

# Private bucket for meal photos. Optional: unset => photo endpoints 400 with a
# clear message and text-only meals still work.
S3_BUCKET = os.getenv('S3_BUCKET')
PHOTO_URL_TTL = 3600  # presigned GET expiry (seconds); every response re-signs

# Uploads are client-side downscaled to ~200-400KB JPEGs; 16MB is the backstop.
MAX_CONTENT_LENGTH = 16 * 1024 * 1024

# --- Google OAuth ------------------------------------------------------------
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:5000/callback')

# --- Accounts ----------------------------------------------------------------
# Emails that bootstrap as admin on their FIRST sign-in only; afterwards status
# lives in the users table and is managed at /admin.
ADMIN_EMAILS = {
    e.strip().lower() for e in os.getenv('ADMIN_EMAILS', '').split(',') if e.strip()
}
MAX_USERS = int(os.getenv('MAX_USERS', '100'))

# Active share links allowed per user (revoked/expired links don't count).
MAX_ACTIVE_SHARES = 20

# Active (unused, unexpired, unrevoked) invite links per user. Bounds how much
# auto-approval any single user can delegate; MAX_USERS is the global backstop.
MAX_ACTIVE_INVITES = 10

# --- AI estimator (optional) -------------------------------------------------
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-5-mini')
OPENAI_CHAT_URL = 'https://api.openai.com/v1/chat/completions'
AI_DAILY_LIMIT = int(os.getenv('AI_DAILY_LIMIT', '10'))  # per user per UTC day

# --- Viscous soluble fiber guide (dietician's handout) -----------------------
# Grams of viscous SOLUBLE fiber per serving; star = >=3g/serving. Single source
# of truth: injected into log.html for the tap-to-add lookup AND embedded in
# the AI estimator prompt. The DynamoDB nutrient key stays 'fiber_g'.
VISCOUS_FIBER_GOAL_G = 20  # Portfolio Diet daily goal; mirrors GOAL_G in the review chart

FIBER_GUIDE = [
    # Beans & legumes
    {'name': 'Kidney beans', 'serving': '1 cup', 'grams': 5.0, 'star': True, 'category': 'Beans & legumes'},
    {'name': 'Black beans', 'serving': '1 cup', 'grams': 4.5, 'star': True, 'category': 'Beans & legumes'},
    {'name': 'Lentils (dal), cooked', 'serving': '1 cup', 'grams': 3.5, 'star': True, 'category': 'Beans & legumes'},
    {'name': 'Chickpeas (chana), cooked', 'serving': '1 cup', 'grams': 3.0, 'star': True, 'category': 'Beans & legumes'},
    {'name': 'Split peas, cooked', 'serving': '1 cup', 'grams': 3.0, 'star': True, 'category': 'Beans & legumes'},
    {'name': 'Mung beans, cooked', 'serving': '1 cup', 'grams': 2.5, 'star': False, 'category': 'Beans & legumes'},
    {'name': 'Edamame, cooked', 'serving': '1 cup', 'grams': 2.0, 'star': False, 'category': 'Beans & legumes'},
    # Starchy vegetables
    {'name': 'Beets, cooked', 'serving': '1 cup', 'grams': 2.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Green peas, cooked', 'serving': '1 cup', 'grams': 2.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Sweet potato with skin', 'serving': '1 medium', 'grams': 2.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Carrots, cooked', 'serving': '1 cup', 'grams': 1.5, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Turnips, cooked', 'serving': '1 cup', 'grams': 1.5, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'White potato with skin', 'serving': '1 medium', 'grams': 1.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Taro, cooked', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Kabocha squash, cooked', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Daikon radish, cooked', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Starchy vegetables'},
    {'name': 'Jicama, raw', 'serving': '1 cup', 'grams': 0.5, 'star': False, 'category': 'Starchy vegetables'},
    # Non-starchy vegetables
    {'name': 'Asparagus, cooked', 'serving': '1 cup', 'grams': 3.5, 'star': True, 'category': 'Non-starchy vegetables'},
    {'name': 'Broccoli, cooked', 'serving': '1 cup', 'grams': 2.0, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Brussels sprouts, cooked', 'serving': '1 cup', 'grams': 1.5, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Green beans, cooked', 'serving': '1 cup', 'grams': 1.5, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Kale, cooked', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Eggplant, cooked', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Nopales, cooked', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Shiitake/wood ear mushrooms, cooked', 'serving': '1 cup', 'grams': 0.5, 'star': False, 'category': 'Non-starchy vegetables'},
    {'name': 'Bok choy, cooked', 'serving': '1 cup', 'grams': 0.5, 'star': False, 'category': 'Non-starchy vegetables'},
    # Grains
    {'name': 'Old fashioned oats, uncooked', 'serving': '1/2 cup', 'grams': 2.0, 'star': False, 'category': 'Grains'},
    {'name': 'Oat bran', 'serving': '1/3 cup', 'grams': 2.0, 'star': False, 'category': 'Grains'},
    {'name': 'Barley, cooked', 'serving': '1/2 cup', 'grams': 1.5, 'star': False, 'category': 'Grains'},
    {'name': 'High-fiber whole wheat bread', 'serving': '1 slice', 'grams': 1.0, 'star': False, 'category': 'Grains'},
    {'name': 'Brown rice, cooked', 'serving': '1 cup', 'grams': 0.5, 'star': False, 'category': 'Grains'},
    # Fats, nuts & seeds
    {'name': 'Psyllium husk powder', 'serving': '1 Tbsp', 'grams': 5.0, 'star': True, 'category': 'Fats, nuts & seeds'},
    {'name': 'Avocado', 'serving': '1/2 fruit', 'grams': 3.5, 'star': True, 'category': 'Fats, nuts & seeds'},
    {'name': 'Basil seeds', 'serving': '1 Tbsp', 'grams': 1.5, 'star': False, 'category': 'Fats, nuts & seeds'},
    {'name': 'Pistachio', 'serving': '1/4 cup', 'grams': 1.0, 'star': False, 'category': 'Fats, nuts & seeds'},
    {'name': 'Almonds', 'serving': '1/4 cup', 'grams': 0.5, 'star': False, 'category': 'Fats, nuts & seeds'},
    {'name': 'Chia seeds', 'serving': '1 Tbsp', 'grams': 0.5, 'star': False, 'category': 'Fats, nuts & seeds'},
    # Fruit
    {'name': 'Pear', 'serving': '1 medium', 'grams': 2.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Persimmon (Fuyu)', 'serving': '1 medium', 'grams': 1.5, 'star': False, 'category': 'Fruit'},
    {'name': 'Blackberries/raspberries', 'serving': '1 cup', 'grams': 1.5, 'star': False, 'category': 'Fruit'},
    {'name': 'Orange', 'serving': '1 medium', 'grams': 1.5, 'star': False, 'category': 'Fruit'},
    {'name': 'Prunes', 'serving': '5 prunes', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Blueberries', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Apple with skin', 'serving': '1 medium', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Strawberries', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Guava', 'serving': '1 fruit', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Loquat', 'serving': '3 medium', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Papaya, cubed', 'serving': '1 cup', 'grams': 1.0, 'star': False, 'category': 'Fruit'},
    {'name': 'Banana', 'serving': '1 medium', 'grams': 0.5, 'star': False, 'category': 'Fruit'},
    {'name': 'Mango, sliced', 'serving': '1 cup', 'grams': 0.5, 'star': False, 'category': 'Fruit'},
]

# --- Per-user tracked nutrient -----------------------------------------------
# Each user tracks ONE micro-nutrient, chosen from the curated catalog below
# (stored on the users row as nutrient_* attrs; only the goal is user-editable,
# prefilled from the catalog default). A closed set keeps the keys safe (they
# double as form field names, map keys, and AI schema properties) and the AI
# estimates grounded. Default = the viscous-fiber experience above.

NUTRIENT_DIRECTIONS = ('at_least', 'at_most')  # maximize toward goal / stay under limit

NUTRIENT_GOAL_MAX = 100000  # write- AND read-side ceiling; mirrors app.py's _NUTRIENT_MAX

# Order = display order in the settings dropdown; the fiber default first.
# Default goals are common adult reference values (RDA/AI or WHO/AHA limits) —
# starting points the user can personalize, not medical advice.
NUTRIENT_CATALOG = [
    {'key': 'fiber_g', 'label': 'viscous fiber', 'unit': 'g',
     'goal': VISCOUS_FIBER_GOAL_G, 'direction': 'at_least'},
    {'key': 'total_fiber_g', 'label': 'total fiber', 'unit': 'g',
     'goal': 30, 'direction': 'at_least'},
    {'key': 'protein_g', 'label': 'protein', 'unit': 'g',
     'goal': 90, 'direction': 'at_least'},
    {'key': 'calories_kcal', 'label': 'calories', 'unit': 'kcal',
     'goal': 2000, 'direction': 'at_most'},
    {'key': 'added_sugar_g', 'label': 'added sugar', 'unit': 'g',
     'goal': 25, 'direction': 'at_most'},
    {'key': 'sodium_mg', 'label': 'sodium', 'unit': 'mg',
     'goal': 2300, 'direction': 'at_most'},
    {'key': 'sat_fat_g', 'label': 'saturated fat', 'unit': 'g',
     'goal': 20, 'direction': 'at_most'},
    {'key': 'potassium_mg', 'label': 'potassium', 'unit': 'mg',
     'goal': 3400, 'direction': 'at_least'},
    {'key': 'calcium_mg', 'label': 'calcium', 'unit': 'mg',
     'goal': 1000, 'direction': 'at_least'},
    {'key': 'iron_mg', 'label': 'iron', 'unit': 'mg',
     'goal': 18, 'direction': 'at_least'},
    {'key': 'cholesterol_mg', 'label': 'cholesterol', 'unit': 'mg',
     'goal': 300, 'direction': 'at_most'},
]

# The fiber default IS the first catalog entry — one definition, no drift.
DEFAULT_NUTRIENT = {**NUTRIENT_CATALOG[0], 'is_default': True}

# Catalog keys double as meal-form field names, nutrients map keys, and AI
# json_schema properties. This import-time check makes a colliding or
# malformed future entry fail at boot instead of corrupting meal writes or
# AI schemas at runtime. 'constructor' would collide with Object.prototype
# in the templates' JS lookups.
_RESERVED_NUTRIENT_KEYS = frozenset((
    'date', 'time', 'description', 'context', 'photo', 'ai_assisted',
    'remove_photo', 'nutrient_key',            # meal-form fields
    'note', 'items', 'amount', 'model',        # AI schema/wire properties
    'constructor',                             # JS prototype pollution
))
assert len({e['key'] for e in NUTRIENT_CATALOG}) == len(NUTRIENT_CATALOG), \
    'NUTRIENT_CATALOG keys must be unique'
for _e in NUTRIENT_CATALOG:
    assert re.fullmatch(r'[a-z][a-z0-9_]*', _e['key']), \
        f"catalog key {_e['key']!r} must be a [a-z0-9_] identifier"
    assert _e['key'] not in _RESERVED_NUTRIENT_KEYS, \
        f"catalog key {_e['key']!r} collides with a reserved name"
    assert _e['direction'] in NUTRIENT_DIRECTIONS and _e['goal'] > 0


def catalog_entry(key):
    """The catalog row for a key, or None (unknown/legacy free-form keys)."""
    for entry in NUTRIENT_CATALOG:
        if entry['key'] == key:
            return entry
    return None


def _safe_goal(raw):
    """A positive finite in-range goal (int when integral) or None."""
    try:
        goal = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(goal) or goal <= 0 or goal > NUTRIENT_GOAL_MAX:
        return None
    return int(goal) if goal.is_integer() else goal


def resolve_nutrient(user_row):
    """The user's tracked-nutrient config: {key,label,unit,goal,direction,is_default}.

    Single source of truth for every consumer (routes, templates, AI prompts).
    No nutrient_key (existing users — no migration) = the fiber default.
    Catalog keys resolve from the CATALOG (label/unit/direction edits reach
    every user immediately); the row contributes only a personalized goal —
    absent or 0 (the follow-the-default sentinel) means the catalog goal.
    Legacy free-form keys (pre-catalog rows) resolve from the row snapshot;
    a corrupt goal there degrades to the safe default (a 0/NaN goal would
    NaN the chart axis math). Goal comes out JSON-safe: int when integral,
    else float — a raw Decimal would blow up in |tojson / jsonify, and a
    blanket float() would render "20.0 g goal".
    """
    key = (user_row or {}).get('nutrient_key')
    if not key:
        return dict(DEFAULT_NUTRIENT)
    entry = catalog_entry(key)
    if entry is not None:
        cfg = {**entry, 'is_default': key == 'fiber_g'}
        goal = _safe_goal(user_row.get('nutrient_goal'))
        if goal is not None:
            cfg['goal'] = goal
        return cfg
    goal = _safe_goal(user_row.get('nutrient_goal'))
    if goal is None:
        return dict(DEFAULT_NUTRIENT)
    direction = user_row.get('nutrient_direction')
    if direction not in NUTRIENT_DIRECTIONS:
        direction = 'at_least'
    return {
        'key': str(key),
        'label': str(user_row.get('nutrient_label') or key),
        'unit': str(user_row.get('nutrient_unit') or ''),
        'goal': goal,
        'direction': direction,
        'is_default': False,
    }


# Dev-only escape hatch: COOKIE_SECURE=0 lets the session cookie work over
# plain-http localhost (Safari rejects Secure cookies there). NEVER set in
# production — TLS-only cookies are assumed by the whole auth design.
COOKIE_SECURE = os.getenv('COOKIE_SECURE', '1') != '0'
