"""In-memory fakes for DynamoDB tables and S3, used by the stub tests.

These implement just the subset of the boto3 resource/client surface that
db.py uses, including boto3 Key/Attr condition evaluation and the string
ConditionExpressions used by the AI counter and share revocation. No AWS
credentials or network access required.
"""
import re
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

from boto3.dynamodb.conditions import AttributeBase, ConditionBase
from botocore.exceptions import ClientError


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
    raise NotImplementedError(f'operator {op}')


def _eval_str_condition(cond, item, values, resolve_name):
    """Evaluate the small string-expression grammar db.py actually uses:
    attribute_[not_]exists(x), a = :v, a <> :v, a < :v, joined by AND / OR."""
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


class FakeBatchWriter:
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


class FakeTable:
    def __init__(self, key_names):
        self.key_names = tuple(key_names)
        self.items = {}

    def _kt(self, mapping):
        return tuple(mapping[k] for k in self.key_names)

    def load(self):
        pass

    def get_item(self, Key):
        item = self.items.get(self._kt(Key))
        return {'Item': deepcopy(item)} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None,
                 ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        if ConditionExpression is not None:
            existing = self.items.get(self._kt(Item))
            cond_item = existing if existing is not None else {}
            names = ExpressionAttributeNames or {}
            if not _eval_str_condition(ConditionExpression, cond_item,
                                       ExpressionAttributeValues or {},
                                       lambda n: names.get(n, n)):
                raise _ccf_error()
        self.items[self._kt(Item)] = deepcopy(Item)
        return {}

    def delete_item(self, Key, ConditionExpression=None,
                    ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        if ConditionExpression is not None:
            existing = self.items.get(self._kt(Key))
            cond_item = existing if existing is not None else {}
            names = ExpressionAttributeNames or {}
            if not _eval_str_condition(ConditionExpression, cond_item,
                                       ExpressionAttributeValues or {},
                                       lambda n: names.get(n, n)):
                raise _ccf_error()
        self.items.pop(self._kt(Key), None)
        return {}

    def scan(self, **kwargs):
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
            # process, so the fake must drop them too or the test would pass
            # on data the real table never returns.
            names = kwargs.get('ExpressionAttributeNames') or {}
            keep = [names.get(a.strip(), a.strip()) for a in projection.split(',')]
            items = [{k: v for k, v in i.items() if k in keep} for i in items]
        return {'Items': items, 'Count': len(items)}

    def query(self, **kwargs):
        cond = kwargs['KeyConditionExpression']
        items = [deepcopy(i) for i in self.items.values() if eval_condition(cond, i)]
        items.sort(key=lambda i: self._kt(i))
        if kwargs.get('ScanIndexForward') is False:
            items.reverse()
        return {'Items': items}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ConditionExpression=None, ExpressionAttributeNames=None,
                    ReturnValues=None):
        kt = self._kt(Key)
        existing = self.items.get(kt)
        target = existing if existing is not None else dict(Key)
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}

        def resolve_name(n):
            return names.get(n, n)

        # Real DynamoDB evaluates the condition against the EXISTING item
        # (absent item -> attribute_exists fails, comparisons fail) — never
        # against the incoming Key, which would make attribute_exists(pk)
        # pass for a missing row. The write still targets `target`.
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
        if ReturnValues == 'ALL_NEW':
            return {'Attributes': deepcopy(target)}
        return {}

    def batch_writer(self):
        return FakeBatchWriter(self)


class FakeMailer:
    """Stands in for mailer.send: captures every message so tests can pull
    verification/reset links out of the bodies. Set .ok = False to simulate
    an SES outage."""

    def __init__(self):
        self.sent = []  # (to_addr, subject, body) tuples, oldest first
        self.ok = True

    def send(self, to_addr, subject, body):
        self.sent.append((to_addr, subject, body))
        return self.ok


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.get_calls = 0  # lets tests assert cache hits vs S3 round-trips

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body.read() if hasattr(Body, 'read') else Body
        return {}

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        if Key not in self.objects:
            raise ClientError(
                {'Error': {'Code': 'NoSuchKey', 'Message': 'The specified key '
                                                           'does not exist.'}},
                'GetObject')
        return {'Body': BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, Bucket, Prefix='', **kwargs):
        contents = [{'Key': k, 'Size': len(self.objects[k])}
                    for k in sorted(self.objects) if k.startswith(Prefix)]
        resp = {'IsTruncated': False}
        if contents:
            resp['Contents'] = contents
        return resp

    def delete_objects(self, Bucket, Delete):
        for obj in Delete['Objects']:
            self.objects.pop(obj['Key'], None)
        return {}


def install(db_module):
    """Swap db.py's AWS handles for in-memory fakes. Call BEFORE importing app."""
    users = FakeTable(('user_id',))
    meals = FakeTable(('user_id', 'sk'))
    shares = FakeTable(('share_token',))
    invites = FakeTable(('invite_token',))
    s3 = FakeS3()
    db_module.users_table = lambda: users
    db_module.meals_table = lambda: meals
    db_module.shares_table = lambda: shares
    db_module.invites_table = lambda: invites
    db_module.ensure_tables = lambda: None
    db_module._s3_client = lambda: s3
    return SimpleNamespace(users=users, meals=meals, shares=shares,
                           invites=invites, s3=s3)
