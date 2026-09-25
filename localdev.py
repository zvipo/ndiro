"""Local development mode (NDIRO_LOCAL_DEV=1): the whole site on one machine,
with no AWS, Google, OpenAI, or SES account.

app.py calls install(app) before its first backend touch, and every cloud
service is swapped for a local stand-in — the same trick the stub tests use,
promoted to a mode a developer can click around in:

  DynamoDB -> MemoryTable: an in-process table with exactly the boto3 surface
              db.py uses (conditional writes, atomic ADD counters,
              projections). With LOCAL_DATA_DIR set it persists as one JSON
              document per table, so restarts and gunicorn --reload keep the
              data. tests/fakes.py imports THIS class: the dev backend and the
              test fakes are one implementation, kept honest by the security
              tests (extend the condition grammar here, not in a copy).
  S3       -> LocalS3: photo bytes as files under LOCAL_DATA_DIR/photos (or
              in memory), keys checked against traversal.
  Google   -> a fake account chooser at /dev/google. auth.build_auth_url and
              auth.fetch_userinfo are the only two touchpoints, so the real
              /login/google -> /callback flow (state CSRF check, MAX_USERS,
              admin bootstrap, invite redemption) runs unchanged.
  SES      -> Mailbox: outgoing mail is kept in memory, shown on /dev with its
              links clickable, and echoed on stdout as DEV_MAIL.
  OpenAI   -> canned estimates (estimate_text / estimate_photo): guide values
              for foods it recognizes, made-up amounts for the rest, and a few
              [tags] in the description to exercise the failure paths.

Plus seed data (personas in every account state, a month of meals with
photos, share and invite links) and the /dev console that ties it together.

Production safety, in layers (invariant #13 in CLAUDE.md):
  - config.py refuses NDIRO_LOCAL_DEV=1 next to any production signal
    (Render's env, an explicit COOKIE_SECURE=1);
  - nothing here runs unless config.LOCAL_DEV is True — the /dev routes do not
    exist otherwise (tests/test_m5_checklist.py asserts /dev is a 404);
  - every page carries a banner and /status says so.
Anyone who can reach the port can sign in as any account, so the port is
bound to loopback only (docker-compose.yml) and must never be published on a
network interface.
"""
import base64
import colorsys
import hashlib
import io
import json
import os
import random
import re
import secrets
import shutil
import textwrap
import threading
import time
from copy import deepcopy
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

from boto3.dynamodb.conditions import AttributeBase
from botocore.exceptions import ClientError
from flask import Blueprint, redirect, render_template, request, session

import ai
import auth
import autolog
import config
import db
import mailer
import native_auth


# =============================================================================
# DynamoDB emulation
# =============================================================================

def _ccf_error():
    return ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException',
                   'Message': 'The conditional request failed'}},
        'UpdateItem')


def eval_condition(cond, item):
    """Evaluate a boto3 Key()/Attr() condition object against an item."""
    expr = cond.get_expression()
    op = expr['operator']
    vals = expr['values']
    if op == 'AND':
        return eval_condition(vals[0], item) and eval_condition(vals[1], item)
    if op == 'OR':
        return eval_condition(vals[0], item) or eval_condition(vals[1], item)
    attr = vals[0]
    assert isinstance(attr, AttributeBase), f'unsupported condition shape: {expr}'
    value = item.get(attr.name)
    if op == '=':
        return value == vals[1]
    if op == '<>':
        return value is not None and value != vals[1]
    if op == 'begins_with':
        return isinstance(value, str) and value.startswith(vals[1])
    if op == 'BETWEEN':
        return value is not None and vals[1] <= value <= vals[2]
    if op in ('<', '<=', '>', '>='):
        if value is None:
            return False
        return {'<': value < vals[1], '<=': value <= vals[1],
                '>': value > vals[1], '>=': value >= vals[1]}[op]
    raise NotImplementedError(f'operator {op}')


def _eval_str_condition(cond, item, values, resolve_name):
    """Evaluate the small string-expression grammar db.py actually uses:
    attribute_[not_]exists(x), a = :v, a <> :v, a < :v, joined by AND / OR
    (DynamoDB precedence: AND binds tighter than OR)."""
    def term(t):
        t = t.strip()
        if t.startswith('attribute_not_exists(') and t.endswith(')'):
            return resolve_name(t[len('attribute_not_exists('):-1].strip()) not in item
        if t.startswith('attribute_exists(') and t.endswith(')'):
            return resolve_name(t[len('attribute_exists('):-1].strip()) in item
        for op in ('<>', '<=', '>=', '=', '<', '>'):
            marker = f' {op} '
            if marker in t:
                left, right = t.split(marker, 1)
                lv = item.get(resolve_name(left.strip()))
                rv = values[right.strip()]
                if lv is None:
                    return False  # DynamoDB: comparisons against missing attrs fail
                return {'=': lv == rv, '<>': lv != rv, '<': lv < rv,
                        '<=': lv <= rv, '>': lv > rv, '>=': lv >= rv}[op]
        raise NotImplementedError(f'condition term: {t}')

    for or_part in cond.split(' OR '):
        if all(term(t) for t in or_part.split(' AND ')):
            return True
    return False


def _json_default(obj):
    """Decimal survives the JSON round trip as a tagged object (DynamoDB
    numbers come back as Decimal, and db.py's Decimal discipline relies on
    it — a plain float would break put_item on the next write)."""
    if isinstance(obj, Decimal):
        return {'$decimal': str(obj)}
    raise TypeError(f'not JSON-serializable: {type(obj).__name__}')


def _json_object_hook(obj):
    if len(obj) == 1 and '$decimal' in obj:
        return Decimal(obj['$decimal'])
    return obj


class _BatchWriter:
    def __init__(self, table):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put_item(self, Item):
        self.table.put_item(Item=Item)

    def delete_item(self, Key):
        self.table.delete_item(Key=Key)


class MemoryTable:
    """One DynamoDB table as a dict keyed by its key tuple.

    path=None keeps it in memory (the tests, and the in-memory dev mode); a
    path persists every write as one JSON document via atomic replace and
    reads it back on construction. The lock makes each call atomic under
    gunicorn's request threads — the conditional-write patterns in db.py
    (the AI counter, the invite claim, the lockout ADD) depend on that.
    Semantics mirror DynamoDB where db.py can tell the difference: conditions
    are evaluated against the EXISTING item (absent -> attribute_exists
    fails), updates upsert, projections drop unlisted attributes.
    """

    def __init__(self, key_names, path=None):
        self.key_names = tuple(key_names)
        self.items = {}
        self.path = path
        self._lock = threading.RLock()
        if path:
            self._load()

    def _kt(self, mapping):
        return tuple(mapping[k] for k in self.key_names)

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as fh:
                rows = json.load(fh, object_hook=_json_object_hook)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            # A half-written or hand-edited file must not wedge every boot:
            # set it aside (nothing is silently lost) and start empty.
            aside = f'{self.path}.corrupt-{int(time.time())}'
            os.replace(self.path, aside)
            print(f"LOCALDEV: could not read {self.path} ({type(e).__name__}); "
                  f"moved it to {aside} and started empty", flush=True)
            return
        for row in rows:
            self.items[self._kt(row)] = row

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f'{self.path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(list(self.items.values()), fh, default=_json_default)
        os.replace(tmp, self.path)

    def clear(self):
        with self._lock:
            self.items.clear()
            self._save()

    def load(self):
        """boto3 Table.load(): the exists-check db.ensure_tables makes."""

    def get_item(self, Key, ConsistentRead=None):
        with self._lock:
            item = self.items.get(self._kt(Key))
            return {'Item': deepcopy(item)} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None,
                 ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        with self._lock:
            if ConditionExpression is not None:
                existing = self.items.get(self._kt(Item))
                cond_item = existing if existing is not None else {}
                names = ExpressionAttributeNames or {}
                if not _eval_str_condition(ConditionExpression, cond_item,
                                           ExpressionAttributeValues or {},
                                           lambda n: names.get(n, n)):
                    raise _ccf_error()
            self.items[self._kt(Item)] = deepcopy(Item)
            self._save()
            return {}

    def delete_item(self, Key, ConditionExpression=None,
                    ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        with self._lock:
            if ConditionExpression is not None:
                existing = self.items.get(self._kt(Key))
                cond_item = existing if existing is not None else {}
                names = ExpressionAttributeNames or {}
                if not _eval_str_condition(ConditionExpression, cond_item,
                                           ExpressionAttributeValues or {},
                                           lambda n: names.get(n, n)):
                    raise _ccf_error()
            self.items.pop(self._kt(Key), None)
            self._save()
            return {}

    def scan(self, **kwargs):
        with self._lock:
            items = [deepcopy(i) for i in self.items.values()]
        fe = kwargs.get('FilterExpression')
        if fe is not None:
            items = [i for i in items if eval_condition(fe, i)]
        if kwargs.get('Select') == 'COUNT':
            return {'Count': len(items)}
        projection = kwargs.get('ProjectionExpression')
        if projection:
            # Real DynamoDB drops every unlisted attribute server-side. The
            # stats scan RELIES on that to keep meal content out of the
            # process, so the emulation must drop them too.
            names = kwargs.get('ExpressionAttributeNames') or {}
            keep = [names.get(a.strip(), a.strip()) for a in projection.split(',')]
            items = [{k: v for k, v in i.items() if k in keep} for i in items]
        return {'Items': items, 'Count': len(items)}

    def query(self, **kwargs):
        cond = kwargs['KeyConditionExpression']
        with self._lock:
            items = [deepcopy(i) for i in self.items.values() if eval_condition(cond, i)]
        items.sort(key=lambda i: self._kt(i))
        if kwargs.get('ScanIndexForward') is False:
            items.reverse()
        limit = kwargs.get('Limit')
        if limit is not None and len(items) > limit:
            # DynamoDB stops after Limit items and hands back where it stopped.
            items = items[:limit]
            return {'Items': items, 'LastEvaluatedKey': {
                k: items[-1][k] for k in self.key_names}}
        return {'Items': items}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ConditionExpression=None, ExpressionAttributeNames=None,
                    ReturnValues=None):
        with self._lock:
            kt = self._kt(Key)
            existing = self.items.get(kt)
            target = existing if existing is not None else dict(Key)
            values = ExpressionAttributeValues or {}
            names = ExpressionAttributeNames or {}

            def resolve_name(n):
                return names.get(n, n)

            # The condition is evaluated against the EXISTING item (absent ->
            # attribute_exists fails, comparisons fail) — never against the
            # incoming Key, which would make attribute_exists(pk) pass for a
            # missing row. The write still targets `target`.
            cond_item = existing if existing is not None else {}
            if ConditionExpression is not None and \
                    not _eval_str_condition(ConditionExpression, cond_item, values, resolve_name):
                raise _ccf_error()

            # Clause parsing: SET / REMOVE / ADD in any combination and order
            # (db.py mixes them, e.g. 'SET email_verified = :t REMOVE ...').
            expr = UpdateExpression.strip()
            parts = [p for p in re.split(r'\b(SET|REMOVE|ADD)\b', expr) if p.strip()]
            if not parts or parts[0] not in ('SET', 'REMOVE', 'ADD'):
                raise NotImplementedError(f'update expression: {expr}')
            for keyword, body in zip(parts[0::2], parts[1::2]):
                if keyword == 'SET':
                    for part in body.split(','):
                        name, val = part.split('=', 1)
                        target[resolve_name(name.strip())] = values[val.strip()]
                elif keyword == 'REMOVE':
                    for name in body.split(','):
                        target.pop(resolve_name(name.strip()), None)
                elif keyword == 'ADD':
                    name, val = body.split()
                    name = resolve_name(name)
                    target[name] = target.get(name, 0) + values[val]
                else:
                    raise NotImplementedError(f'update expression: {expr}')
            self.items[kt] = target
            self._save()
            if ReturnValues == 'ALL_NEW':
                return {'Attributes': deepcopy(target)}
            return {}

    def batch_writer(self):
        return _BatchWriter(self)


# =============================================================================
# S3 emulation
# =============================================================================

class LocalS3:
    """The S3 client surface db.py uses, on local disk (root) or in memory.

    Keys are server-built (users/{user_id}/meals/{date}/{meal_id}.jpg), but
    the disk mode still refuses anything that could leave the root — the
    emulation must not be the one place a bad key becomes a path.
    """

    def __init__(self, root=None):
        self.root = os.path.abspath(root) if root else None
        self.objects = {}      # in-memory store (tests inspect it directly)
        self.get_calls = 0     # tests assert LRU hits vs round-trips
        self._lock = threading.RLock()
        if self.root:
            os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        parts = key.split('/')
        if not key or key.startswith('/') or any(p in ('', '.', '..') for p in parts):
            raise ValueError('refusing an unsafe object key')
        return os.path.join(self.root, *parts)

    @staticmethod
    def _no_such_key():
        return ClientError(
            {'Error': {'Code': 'NoSuchKey',
                       'Message': 'The specified key does not exist.'}},
            'GetObject')

    def put_object(self, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, 'read') else bytes(Body)
        with self._lock:
            if self.root is None:
                self.objects[Key] = data
                return {}
            path = self._path(Key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f'{path}.tmp'
            with open(tmp, 'wb') as fh:
                fh.write(data)
            os.replace(tmp, path)
        return {}

    def get_object(self, Bucket, Key):
        with self._lock:
            self.get_calls += 1
            if self.root is None:
                if Key not in self.objects:
                    raise self._no_such_key()
                return {'Body': io.BytesIO(self.objects[Key])}
            try:
                with open(self._path(Key), 'rb') as fh:
                    return {'Body': io.BytesIO(fh.read())}
            except (FileNotFoundError, NotADirectoryError, ValueError):
                raise self._no_such_key()

    def delete_object(self, Bucket, Key):
        with self._lock:
            if self.root is None:
                self.objects.pop(Key, None)
                return {}
            try:
                path = self._path(Key)
                os.remove(path)
            except (FileNotFoundError, NotADirectoryError, ValueError):
                return {}
            # Prune now-empty directories so the photo tree mirrors S3's
            # key space (no ghost prefixes).
            d = os.path.dirname(path)
            while d != self.root:
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)
        return {}

    def delete_objects(self, Bucket, Delete):
        for obj in Delete['Objects']:
            self.delete_object(Bucket, obj['Key'])
        return {}

    def keys(self):
        """Every stored key, in S3's (codepoint) listing order."""
        with self._lock:
            if self.root is None:
                return sorted(self.objects)
            found = []
            for dirpath, _dirs, files in os.walk(self.root):
                rel = os.path.relpath(dirpath, self.root)
                for name in files:
                    if name.endswith('.tmp'):
                        continue
                    key = name if rel == '.' else f'{rel}/{name}'
                    found.append(key.replace(os.sep, '/'))
            return sorted(found)

    def _size(self, key):
        if self.root is None:
            return len(self.objects[key])
        return os.path.getsize(self._path(key))

    def list_objects_v2(self, Bucket, Prefix='', **kwargs):
        contents = [{'Key': k, 'Size': self._size(k)}
                    for k in self.keys() if k.startswith(Prefix)]
        resp = {'IsTruncated': False}
        if contents:
            resp['Contents'] = contents
        return resp

    def clear(self):
        with self._lock:
            self.objects.clear()
            if self.root:
                for name in os.listdir(self.root):
                    path = os.path.join(self.root, name)
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)


# =============================================================================
# Mail: the dev-mode inbox
# =============================================================================

class Mailbox:
    """Stands in for mailer.send: keeps the last LIMIT messages for the /dev
    console and echoes each one on stdout. This IS the inbox — the
    verification and reset links are meant to be read here, which is the
    opposite of the production logging rule (invariant #8) and exactly why
    it exists only behind NDIRO_LOCAL_DEV=1."""

    LIMIT = 100
    _LINK_RE = re.compile(r'https?://[^\s<>"]+')

    def __init__(self, echo=True):
        self.messages = []  # oldest first
        self.echo = echo
        self.ok = True      # False simulates a mail outage (send returns False)
        self._lock = threading.Lock()

    def send(self, to_addr, subject, body):
        if not self.ok:
            return False
        msg = {
            'to': to_addr,
            'subject': subject,
            'body': body,
            'at': datetime.now(timezone.utc).strftime('%H:%M:%S UTC'),
            'links': self._LINK_RE.findall(body),
        }
        with self._lock:
            self.messages.append(msg)
            del self.messages[:-self.LIMIT]
        if self.echo:
            print(f"DEV_MAIL to={to_addr} subject={subject!r}\n"
                  + textwrap.indent(body.rstrip(), '    '), flush=True)
        return True

    def clear(self):
        with self._lock:
            self.messages.clear()


# =============================================================================
# Google sign-in stand-in
# =============================================================================
# Fictional accounts on RFC 2606 example addresses. Each is a Google-style
# account with a stable `sub`; the seed creates them in the listed status, and
# the chooser page offers them like Google's own account picker.

PERSONAS = (
    {'key': 'admin', 'sub': 'dev-admin', 'email': config.LOCAL_DEV_ADMIN_EMAIL,
     'name': 'Dev Admin', 'status': 'admin',
     'blurb': 'Admin — approve and reject accounts at /admin, watch /admin/monitor.'},
    {'key': 'tendai', 'sub': 'dev-tendai', 'email': 'tendai@example.com',
     'name': 'Tendai Moyo', 'status': 'approved',
     'blurb': 'Approved — a month of seeded meals and photos, a share link, an open invite.'},
    {'key': 'rudo', 'sub': 'dev-rudo', 'email': 'rudo@example.com',
     'name': 'Rudo Banda', 'status': 'approved',
     'blurb': 'Approved — tracks protein instead of fiber (the custom-micro UI), a few meals.'},
    {'key': 'farai', 'sub': 'dev-farai', 'email': 'farai@example.com',
     'name': 'Farai Ncube', 'status': 'pending',
     'blurb': 'Pending — lands on the waiting page until the admin approves.'},
    {'key': 'blessing', 'sub': 'dev-blessing', 'email': 'blessing@example.com',
     'name': 'Blessing Dube', 'status': 'rejected',
     'blurb': 'Rejected — sign-in bounces straight back to the landing page.'},
)

# One native (email/password) account, verified and approved, so the password
# form on /login can be tried without going through signup + the mailbox.
NATIVE_PERSONA = {
    'email': 'nyasha@example.com', 'name': 'Nyasha Chirwa',
    'password': 'password123',
    'blurb': 'Email/password account (verified, approved) — use the form on /login.',
}


def _avatar(name, seed):
    """A data-URI SVG avatar (initials on a colored disc) so the fake accounts
    look like real ones in the corner menu and on the share/invite pages."""
    hue = (int(hashlib.sha256(seed.encode()).hexdigest()[:6], 16) % 360) / 360
    r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.72))
    initials = ''.join(w[0] for w in name.split()[:2]).upper() or '?'
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           f"<circle cx='32' cy='32' r='32' fill='rgb({r},{g},{b})'/>"
           f"<text x='32' y='41' text-anchor='middle' font-family='sans-serif' "
           f"font-size='26' font-weight='600' fill='#fff'>{initials}</text></svg>")
    # '#' must be encoded: inside a data: URI it would start a fragment.
    return 'data:image/svg+xml;utf8,' + quote(svg, safe="/:='<>,.")


def persona_info(persona):
    """What Google's userinfo would return for this persona."""
    return {'sub': persona['sub'], 'email': persona['email'],
            'name': persona['name'], 'picture': _avatar(persona['name'], persona['sub'])}


def build_auth_url(state):
    """auth.build_auth_url stand-in: the chooser page instead of Google."""
    return '/dev/google?state=' + quote(state, safe='')


def encode_custom(email, name):
    payload = json.dumps({'e': email, 'n': name}).encode()
    return 'c.' + base64.urlsafe_b64encode(payload).decode().rstrip('=')


def fetch_userinfo(code):
    """auth.fetch_userinfo stand-in: the 'authorization code' names a persona
    (p.<key>) or carries a custom account (c.<base64 json>)."""
    code = code or ''
    if code.startswith('p.'):
        for persona in PERSONAS:
            if persona['key'] == code[2:]:
                return persona_info(persona), None
        return None, 'Unknown local-dev persona'
    if code.startswith('c.'):
        raw = code[2:]
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        except (ValueError, TypeError):
            return None, 'Malformed local-dev sign-in code'
        email, err = native_auth.valid_email(data.get('e') if isinstance(data, dict) else None)
        if err:
            return None, err
        name = str(data.get('n') or '').strip()[:80] or email.split('@')[0]
        # Deterministic per email, so signing in again reaches the same account.
        sub = 'dev-' + hashlib.sha256(email.encode()).hexdigest()[:12]
        return {'sub': sub, 'email': email, 'name': name,
                'picture': _avatar(name, sub)}, None
    return None, 'Unknown local-dev sign-in code'


# =============================================================================
# AI estimator stand-in
# =============================================================================
# Canned but not dumb: foods the dietician guide knows get their guide value
# (so "lentil dal with brown rice" reads sensibly), everything else gets a
# small made-up amount, and the note says so. Tags in the description drive
# the failure paths the UI has to handle.

AI_TRIGGERS = (
    ('[fail]', 'upstream error — 502, the AI use is refunded'),
    ('[garbage]', 'unreadable answer — 502, NOT refunded (the call was billed)'),
    ('[slow]', 'a 4-second answer (the busy state)'),
)

# (regex, guide name) in match order: specific phrases before the generic
# word they contain ("sweet potato" before "potato", "oat bran" before "oat").
_FOOD_KEYWORDS = (
    (r'kidney bean', 'Kidney beans'),
    (r'black bean', 'Black beans'),
    (r'lentil|\bd(?:aa|ha|a)l\b', 'Lentils (dal), cooked'),
    (r'chickpea|chana|garbanzo|hummus', 'Chickpeas (chana), cooked'),
    (r'split pea', 'Split peas, cooked'),
    (r'mung', 'Mung beans, cooked'),
    (r'edamame', 'Edamame, cooked'),
    (r'beet', 'Beets, cooked'),
    (r'green pea|\bpeas?\b', 'Green peas, cooked'),
    (r'sweet potato|kumara', 'Sweet potato with skin'),
    (r'carrot', 'Carrots, cooked'),
    (r'turnip', 'Turnips, cooked'),
    (r'potato', 'White potato with skin'),
    (r'\btaro\b', 'Taro, cooked'),
    (r'kabocha|squash|pumpkin', 'Kabocha squash, cooked'),
    (r'daikon|radish', 'Daikon radish, cooked'),
    (r'jicama', 'Jicama, raw'),
    (r'asparagus', 'Asparagus, cooked'),
    (r'broccoli', 'Broccoli, cooked'),
    (r'brussels', 'Brussels sprouts, cooked'),
    (r'green bean', 'Green beans, cooked'),
    (r'\bkale\b|greens|spinach|rape\b|covo', 'Kale, cooked'),
    (r'eggplant|aubergine', 'Eggplant, cooked'),
    (r'nopal', 'Nopales, cooked'),
    (r'mushroom|shiitake', 'Shiitake/wood ear mushrooms, cooked'),
    (r'bok choy|pak choi', 'Bok choy, cooked'),
    (r'oat bran', 'Oat bran'),
    (r'\boats?\b|oatmeal|porridge', 'Old fashioned oats, uncooked'),
    (r'barley', 'Barley, cooked'),
    (r'bread|toast|sandwich', 'High-fiber whole wheat bread'),
    (r'brown rice', 'Brown rice, cooked'),
    (r'psyllium', 'Psyllium husk powder'),
    (r'avocado|guacamole', 'Avocado'),
    (r'basil seed', 'Basil seeds'),
    (r'pistachio', 'Pistachio'),
    (r'almond', 'Almonds'),
    (r'\bchia\b', 'Chia seeds'),
    (r'\bpears?\b', 'Pear'),
    (r'persimmon', 'Persimmon (Fuyu)'),
    (r'blackberr|raspberr', 'Blackberries/raspberries'),
    (r'orange', 'Orange'),
    (r'prune', 'Prunes'),
    (r'blueberr', 'Blueberries'),
    (r'apple', 'Apple with skin'),
    (r'strawberr', 'Strawberries'),
    (r'guava', 'Guava'),
    (r'loquat', 'Loquat'),
    (r'papaya|pawpaw', 'Papaya, cubed'),
    (r'banana', 'Banana'),
    (r'mango', 'Mango, sliced'),
)
_GUIDE = {f['name']: f for f in config.FIBER_GUIDE}
for _pattern, _name in _FOOD_KEYWORDS:
    assert _name in _GUIDE, f'localdev keyword table names unknown guide food {_name!r}'

# Stock answers for photos: (description, guide foods pictured).
STOCK_PHOTO_MEALS = (
    ('Lentil dal with brown rice and steamed broccoli',
     ('Lentils (dal), cooked', 'Brown rice, cooked', 'Broccoli, cooked')),
    ('Oatmeal with blueberries, chia seeds and half a banana',
     ('Old fashioned oats, uncooked', 'Blueberries', 'Chia seeds', 'Banana')),
    ('Black bean tacos with avocado and salsa',
     ('Black beans', 'Avocado')),
    ('Chickpea and carrot salad with a slice of whole wheat bread',
     ('Chickpeas (chana), cooked', 'Carrots, cooked', 'High-fiber whole wheat bread')),
    ('Roasted sweet potato and kale bowl with pistachios',
     ('Sweet potato with skin', 'Kale, cooked', 'Pistachio')),
    ('Barley and asparagus risotto with mushrooms',
     ('Barley, cooked', 'Asparagus, cooked', 'Shiitake/wood ear mushrooms, cooked')),
)

_TAG_RE = re.compile(r'\[(fail|garbage|slow)\]', re.IGNORECASE)
_SPLIT_RE = re.compile(r',|;|/|\+|&|\bwith\b|\band\b|\bplus\b|\bon\b', re.IGNORECASE)
# Words that describe a portion or a preparation, not a food: dropped from the
# leftover phrases so "a bowl of" or "grilled" never becomes an item by itself.
_FILLER_RE = re.compile(
    r'\b(a|an|the|some|of|cup|cups|bowl|plate|slice|slices|handful|piece|pieces|'
    r'large|small|big|half|grilled|baked|steamed|fried|roasted|boiled|cooked|'
    r'fresh|sliced|chopped|mixed|homemade|leftover|leftovers|serving|portion)\b')


def _hash_pick(text, n):
    return int(hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:8], 16) % n


def _custom_amount(cfg, seed_text):
    """A made-up per-item amount for a non-fiber micro: 4-23% of the goal,
    stable for the same text so repeated estimates agree."""
    return round(cfg['goal'] * (4 + _hash_pick(seed_text, 20)) / 100, 1)


def _phrases(text):
    out = []
    for piece in _SPLIT_RE.split(text):
        piece = re.sub(r'[^a-z0-9 \'-]', ' ', piece.lower())
        piece = re.sub(r'\s+', ' ', _FILLER_RE.sub(' ', piece)).strip()
        if len(re.sub(r'[^a-z]', '', piece)) >= 3:
            out.append(piece)
    return out[:8]


def _items_for_text(description, cfg):
    """(items, note) for a description. Fiber: guide lookups + small made-up
    amounts for leftovers. Custom micro: made-up amounts per food phrase."""
    text = description.lower()
    items = []
    if cfg['is_default']:
        used = set()
        for pattern, name in _FOOD_KEYWORDS:
            if name in used:
                continue
            if not re.search(pattern, text):
                continue
            used.add(name)
            food = _GUIDE[name]
            items.append({'food': food['name'], 'serving': food['serving'],
                          'amount': float(food['grams'])})
            # Blank every occurrence so a generic word inside a specific
            # phrase ("potato" in "sweet potato") is not counted twice and
            # an alias ("dal" next to "lentil") never becomes a leftover.
            text = re.sub(pattern, lambda m: ' ' * len(m.group()), text)
        for phrase in _phrases(text):
            items.append({'food': phrase.capitalize(), 'serving': '1 serving',
                          'amount': (0.0, 0.5, 1.0)[_hash_pick(phrase, 3)]})
        note = ('Stub estimate (local dev): guide values for recognized foods, '
                'made-up amounts for the rest; portions are not scaled.')
    else:
        for phrase in _phrases(text) or ['meal']:
            items.append({'food': phrase.capitalize(), 'serving': '1 serving',
                          'amount': _custom_amount(cfg, phrase)})
        note = f"Stub estimate (local dev): made-up {cfg['label']} amounts."
    return items[:20], note


def _result(items, note, cfg, description=None):
    result = {
        'amount': max(round(sum(i['amount'] for i in items), 1), 0.0),
        'items': items,
        'note': note,
        'model': config.OPENAI_MODEL,
    }
    if description is not None:
        result['description'] = description
    return result


def _fail(kind, cfg, mode, log_context):
    """The two failure shapes app.py handles, logged like the real thing so
    the [ref ...] in the UI matches an AI_ERROR line."""
    ctx = {'mode': mode, 'nutrient': cfg['key'], 'stub': True, **(log_context or {})}
    if kind == 'fail':
        ref = ai.log_failure('http', {**ctx, 'status': 503,
                                      'message': 'failure requested with [fail]'})
        return None, ('AI estimate failed (upstream error)', 502, True, ref)
    ref = ai.log_failure('parse', {**ctx, 'error': 'KeyError',
                                   'detail': 'garbage requested with [garbage]',
                                   'expected_key': cfg['key']})
    return None, ('AI estimate returned an unreadable response', 502, False, ref)


def estimate_text(description, cfg, log_context=None):
    """ai.estimate_text stand-in — same (result, None) / (None, err) contract."""
    tags = {t.lower() for t in _TAG_RE.findall(description)}
    if 'slow' in tags:
        time.sleep(4)
    if 'fail' in tags:
        return _fail('fail', cfg, 'text', log_context)
    if 'garbage' in tags:
        return _fail('garbage', cfg, 'text', log_context)
    items, note = _items_for_text(_TAG_RE.sub(' ', description), cfg)
    return _result(items, note, cfg), None


def estimate_photo(photo_bytes, cfg, log_context=None, history=None):
    """ai.estimate_photo stand-in: one of the stock meals, chosen by the photo
    bytes so the same photo always gets the same answer."""
    description, foods = STOCK_PHOTO_MEALS[
        int(hashlib.sha1(photo_bytes).hexdigest()[:8], 16) % len(STOCK_PHOTO_MEALS)]
    if cfg['is_default']:
        items = [{'food': _GUIDE[n]['name'], 'serving': _GUIDE[n]['serving'],
                  'amount': float(_GUIDE[n]['grams'])} for n in foods]
        note = 'Stub estimate (local dev): a stock meal, not what is in the photo.'
    else:
        items = [{'food': _GUIDE[n]['name'], 'serving': _GUIDE[n]['serving'],
                  'amount': _custom_amount(cfg, n)} for n in foods]
        note = (f"Stub estimate (local dev): a stock meal with made-up "
                f"{cfg['label']} amounts.")
    return _result(items, note, cfg, description=description), None


# =============================================================================
# Seed data
# =============================================================================

# Tendai's menu: (slot, description, context, ((guide food, servings), ...)).
# Fiber per meal is computed from the guide, so the review chart shows real
# hits and misses against the 20 g goal.
_MENU = (
    ('breakfast', 'Oatmeal with blueberries and chia seeds', None,
     (('Old fashioned oats, uncooked', 1), ('Blueberries', 0.5), ('Chia seeds', 1))),
    ('breakfast', 'Whole wheat toast with avocado', 'Quick one before work',
     (('High-fiber whole wheat bread', 2), ('Avocado', 1))),
    ('breakfast', 'Oat bran porridge with a sliced banana', None,
     (('Oat bran', 1), ('Banana', 1))),
    ('breakfast', 'Scrambled eggs on toast, an orange', None,
     (('High-fiber whole wheat bread', 1), ('Orange', 1))),
    ('lunch', 'Lentil dal with brown rice', 'Leftovers from last night',
     (('Lentils (dal), cooked', 1), ('Brown rice, cooked', 1))),
    ('lunch', 'Chickpea salad with carrots and greens', None,
     (('Chickpeas (chana), cooked', 1), ('Carrots, cooked', 0.5))),
    ('lunch', 'Black bean tacos with avocado', 'Ate out',
     (('Black beans', 0.5), ('Avocado', 0.5))),
    ('lunch', 'Split pea soup with a slice of bread', None,
     (('Split peas, cooked', 1), ('High-fiber whole wheat bread', 1))),
    ('snack', 'Apple with a handful of almonds', None,
     (('Apple with skin', 1), ('Almonds', 1))),
    ('snack', 'A pear', None, (('Pear', 1),)),
    ('snack', 'Psyllium husk in water', 'Before dinner',
     (('Psyllium husk powder', 1),)),
    ('snack', 'Strawberries', None, (('Strawberries', 1),)),
    ('dinner', 'Kidney bean stew with sweet potato', 'Big portion',
     (('Kidney beans', 1), ('Sweet potato with skin', 1))),
    ('dinner', 'Barley risotto with asparagus and mushrooms', None,
     (('Barley, cooked', 1), ('Asparagus, cooked', 1),
      ('Shiitake/wood ear mushrooms, cooked', 0.5))),
    ('dinner', 'Sadza with mixed greens and mung beans', 'Sunday dinner at home',
     (('Kale, cooked', 1), ('Mung beans, cooked', 1))),
    ('dinner', 'Grilled fish with broccoli and a baked potato', None,
     (('Broccoli, cooked', 1), ('White potato with skin', 1))),
    ('dinner', 'Edamame and brown rice bowl with bok choy', None,
     (('Edamame, cooked', 1), ('Brown rice, cooked', 1), ('Bok choy, cooked', 1))),
)
for _slot, _desc, _ctx, _foods in _MENU:
    for _name, _qty in _foods:
        assert _name in _GUIDE, f'localdev seed menu names unknown guide food {_name!r}'

# Rudo tracks protein: (slot, description, grams).
_PROTEIN_MENU = (
    ('breakfast', 'Greek yoghurt with granola', 18),
    ('lunch', 'Chicken and rice bowl', 38),
    ('dinner', 'Beef stew with sadza', 42),
    ('snack', 'Peanut butter on toast', 9),
    ('lunch', 'Bean and egg burrito', 24),
    ('dinner', 'Grilled bream with vegetables', 35),
)

_SLOTS = ('breakfast', 'lunch', 'snack', 'dinner')
_SLOT_WINDOWS = {'breakfast': (7 * 60 + 20, 8 * 60 + 45), 'lunch': (12 * 60, 13 * 60 + 40),
                 'snack': (15 * 60 + 30, 16 * 60 + 45), 'dinner': (18 * 60 + 30, 20 * 60 + 30)}


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: bitmap default only
        return ImageFont.load_default()


def placeholder_jpeg(title, seed, size=(640, 480)):
    """A generated 'meal photo': gradient, a plate with colored blobs, and the
    meal name — enough to exercise thumbnails, the lightbox, the proxy, and
    the cache without shipping real photos in the repo."""
    from PIL import Image, ImageDraw
    rng = random.Random(seed)
    w, h = size
    hue = rng.random()
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.35, 0.6))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.5, 0.35))
    img = Image.new('RGB', size, top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    draw.ellipse([w * 0.2, h * 0.12, w * 0.8, h * 0.82], fill=(245, 240, 230),
                 outline=(200, 195, 185), width=4)
    for _ in range(rng.randint(3, 6)):
        cx, cy = rng.uniform(w * 0.33, w * 0.67), rng.uniform(h * 0.28, h * 0.66)
        r = rng.uniform(25, 60)
        fill = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(rng.random(), 0.6, 0.72))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    font = _font(26)
    text = title[:48]
    left, top_y, right, bottom_y = draw.textbbox((0, 0), text, font=font)
    tw, th = right - left, bottom_y - top_y
    draw.rectangle([0, h - th - 26, w, h], fill=(30, 30, 30))
    draw.text(((w - tw) / 2 - left, h - th - 16 - top_y), text, fill=(240, 235, 220), font=font)
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=82)
    return out.getvalue()


def _put_meal(user_id, day, minute_of_day, description, nutrients, rng,
              context=None, ai_assisted=False, photo=False):
    hh, mm = divmod(minute_of_day, 60)
    meal_id = f'{hh:02d}{mm:02d}00-{rng.getrandbits(24):06x}'
    stamp = datetime.combine(day, dtime(hh, mm), tzinfo=timezone.utc).isoformat()
    item = {
        'user_id': user_id,
        'sk': db.meal_sk(day.isoformat(), meal_id),
        'date': day.isoformat(),
        'meal_id': meal_id,
        'description': description,
        'nutrients': {k: Decimal(str(v)) for k, v in nutrients.items()},
        'created_at': stamp,
        'updated_at': stamp,
    }
    if context:
        item['context'] = context
    if ai_assisted:
        item['ai_assisted'] = True
    if photo and config.S3_BUCKET:
        key = db.photo_key(user_id, day.isoformat(), meal_id)
        db.put_photo(io.BytesIO(placeholder_jpeg(description, meal_id)), key)
        item['photo_key'] = key
        item['photo_v'] = stamp
    db.put_meal(item)


def _seed_fiber_meals(user_id, today, rng):
    for offset in range(27, -1, -1):
        day = today - timedelta(days=offset)
        if offset and rng.random() < 0.12:
            continue  # an empty day now and then, like a real log
        slots = sorted(rng.sample(_SLOTS, rng.choice((2, 3, 3, 4))), key=_SLOTS.index)
        for slot in slots:
            _slot, description, context, foods = rng.choice(
                [m for m in _MENU if m[0] == slot])
            grams = round(sum(_GUIDE[name]['grams'] * qty for name, qty in foods), 1)
            photo = rng.random() < 0.3
            _put_meal(user_id, day, rng.randint(*_SLOT_WINDOWS[slot]), description,
                      {'fiber_g': grams}, rng, context=context,
                      ai_assisted=photo or rng.random() < 0.3, photo=photo)


def _seed_protein_meals(user_id, today, rng):
    for offset in range(4, -1, -1):
        day = today - timedelta(days=offset)
        for slot, description, grams in rng.sample(_PROTEIN_MENU, rng.choice((1, 2))):
            _put_meal(user_id, day, rng.randint(*_SLOT_WINDOWS[slot]), description,
                      {'protein_g': grams}, rng)


def _wipe():
    for table in (TABLES.users, TABLES.meals, TABLES.shares, TABLES.invites):
        table.clear()
    S3.clear()
    db._photo_cache.drop_prefix('users/')
    spool = autolog.spool_dir()
    for name in os.listdir(spool):
        try:
            os.remove(os.path.join(spool, name))
        except OSError:
            pass
    MAILBOX.clear()


def seed(force=False):
    """Populate an EMPTY store — or, with force, wipe everything (tables,
    photos, the auto-log spool, the mailbox) and start over. Deterministic:
    the same meals, times, and photos every time. Returns True when it ran."""
    if TABLES.users.items and not force:
        return False
    _wipe()
    rng = random.Random(20260101)
    today = datetime.now(timezone.utc).date()
    for persona in PERSONAS:
        info = persona_info(persona)
        db.create_user(info['sub'], info['email'], info['name'], persona['status'],
                       info['picture'])
    db.set_user_nutrient('dev-rudo', 'protein_g', 'protein', 'g', 0, 'at_least')

    native = NATIVE_PERSONA
    uid = native_auth.new_user_id(native['email'])
    _raw, token_hash = native_auth.mint_token()
    db.create_native_user(uid, native['email'], native['name'],
                          native_auth.hash_password(native['password']),
                          token_hash, int(time.time()) + native_auth.VERIFY_TTL_S)
    db.mark_email_verified(uid, token_hash)
    db.set_user_status(uid, 'approved')

    _seed_fiber_meals('dev-tendai', today, rng)
    _seed_protein_meals('dev-rudo', today, rng)

    db.create_share('dev-tendai', 'Dietician', None)
    old = db.create_share('dev-tendai', 'Old link', None)
    db.revoke_share(old['share_token'], 'dev-tendai')
    db.create_invite('dev-tendai', int(time.time()) + 7 * 86400, 'For a friend')
    return True


# =============================================================================
# The /dev console
# =============================================================================

bp = Blueprint('localdev', __name__)


def _form_token():
    """Same session-bound token app.py's auth forms use (kept in step by
    hand — importing app here would be circular)."""
    token = session.get('form_token')
    if not token:
        token = secrets.token_urlsafe(16)
        session['form_token'] = token
    return token


def _check_form_token():
    expected = session.get('form_token')
    return bool(expected) and request.form.get('form_token') == expected


def _accounts():
    """Personas with their LIVE status (None = no row yet)."""
    out = []
    for persona in PERSONAS:
        row = db.get_user(persona['sub'])
        out.append({**persona, 'picture': _avatar(persona['name'], persona['sub']),
                    'status': row.get('status') if row else None})
    return out


@bp.route('/dev')
def dev_console():
    row = db.find_user_by_email(NATIVE_PERSONA['email'], provider='native')
    native = {**NATIVE_PERSONA, 'status': row.get('status') if row else None,
              'picture': _avatar(NATIVE_PERSONA['name'], NATIVE_PERSONA['email'])}
    return render_template(
        'dev_console.html', user=auth.current_user(), accounts=_accounts(),
        native=native, messages=list(reversed(MAILBOX.messages)),
        ai_mode=config.LOCAL_DEV_AI, triggers=AI_TRIGGERS,
        data_dir=config.LOCAL_DATA_DIR, photos_enabled=bool(config.S3_BUCKET),
        email_enabled=config.EMAIL_ENABLED, ai_daily_limit=config.AI_DAILY_LIMIT,
        form_token=_form_token())


def _render_chooser(state, error=None, email='', name=''):
    return render_template('dev_google.html', user=None, accounts=_accounts(),
                           state=state, error=error, email=email, name=name)


@bp.route('/dev/login/<key>')
def dev_login(key):
    """One-click sign-in from the console: the same three session writes
    /login/google makes, then straight into the REAL /callback with the
    persona's code — the chooser page is for the /login-button path, where
    it stands in for Google's own account picker."""
    if not any(persona['key'] == key for persona in PERSONAS):
        return 'Unknown persona', 404
    session['login_next'] = auth._safe_next(request.args.get('next'), default='/log')
    session.pop('invite_token', None)
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    return redirect(f'/callback?state={quote(state, safe="")}&code=p.{key}')


@bp.route('/dev/google')
def dev_google():
    """Where the fake 'Sign in with Google' lands: an account chooser. Each
    entry links to the REAL /callback with the state it arrived with."""
    return _render_chooser(request.args.get('state', ''))


@bp.route('/dev/google', methods=['POST'])
def dev_google_custom():
    """'Use another account': any address becomes a Google-style account."""
    state = request.form.get('state', '')
    name = (request.form.get('name') or '').strip()
    email, err = native_auth.valid_email(request.form.get('email'))
    if err:
        return _render_chooser(state, error=err,
                               email=request.form.get('email', ''), name=name)
    return redirect(f'/callback?state={quote(state, safe="")}'
                    f'&code={encode_custom(email, name)}')


@bp.route('/dev/reset', methods=['POST'])
def dev_reset():
    if not _check_form_token():
        return 'Invalid form token — reload /dev and retry.', 400
    seed(force=True)
    return redirect('/dev')


@bp.route('/dev/mail/clear', methods=['POST'])
def dev_mail_clear():
    if not _check_form_token():
        return 'Invalid form token — reload /dev and retry.', 400
    MAILBOX.clear()
    return redirect('/dev#mail')


# =============================================================================
# Wiring
# =============================================================================

TABLES = None
S3 = None
MAILBOX = Mailbox()


def make_tables(directory=None):
    """The four tables; with a directory, each persists as <name>.json."""
    def path(name):
        return os.path.join(directory, f'{name}.json') if directory else None
    return SimpleNamespace(
        users=MemoryTable(('user_id',), path('users')),
        meals=MemoryTable(('user_id', 'sk'), path('meals')),
        shares=MemoryTable(('share_token',), path('shares')),
        invites=MemoryTable(('invite_token',), path('invites')),
    )


def install(app):
    """Swap every cloud backend for its local stand-in, mount /dev, seed.
    Called by app.py — before db.ensure_tables(), which it replaces."""
    global TABLES, S3
    data_dir = config.LOCAL_DATA_DIR
    TABLES = make_tables(os.path.join(data_dir, 'tables') if data_dir else None)
    S3 = LocalS3(os.path.join(data_dir, 'photos') if data_dir else None)

    db.users_table = lambda: TABLES.users
    db.meals_table = lambda: TABLES.meals
    db.shares_table = lambda: TABLES.shares
    db.invites_table = lambda: TABLES.invites
    db.ensure_tables = lambda: None
    db._s3_client = lambda: S3
    mailer.send = MAILBOX.send
    auth.build_auth_url = build_auth_url
    auth.fetch_userinfo = fetch_userinfo
    if config.LOCAL_DEV_AI == 'stub':
        ai.estimate_text = estimate_text
        ai.estimate_photo = estimate_photo
    app.register_blueprint(bp)

    seeded = seed() if config.LOCAL_DEV_SEED else False
    port = os.getenv('PORT', '8000')
    ai_line = {'stub': 'canned answers (see /dev for the [tags])',
               'off': 'off — buttons hidden',
               'real': 'REAL OpenAI calls with OPENAI_API_KEY'}[config.LOCAL_DEV_AI]
    print(textwrap.dedent(f"""\
        ==================================================================
         NDIRO LOCAL DEV MODE — no AWS, Google, OpenAI, or SES is used
           console   http://localhost:{port}/dev
           data      {data_dir + ' (persists across restarts)' if data_dir else 'in memory (lost on restart)'}
           sign-in   fake Google account chooser + email/password
           email     {'kept in the /dev mailbox (also echoed here as DEV_MAIL)' if config.EMAIL_ENABLED else 'off'}
           photos    {'local files' if config.S3_BUCKET else 'off'}
           AI        {ai_line}
           seed      {'loaded' if seeded else 'skipped (data present)' if config.LOCAL_DEV_SEED else 'disabled'}
        =================================================================="""), flush=True)
