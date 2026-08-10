"""DynamoDB + S3 access for Ndiro.

Multi-tenant discipline: every meal/share accessor takes an explicit user_id
that callers must resolve from the session (never from URL/query/form input),
and S3 keys are constructed here, server-side only, under users/{user_id}/.

Tables (all on-demand, auto-created on boot, no GSIs — deliberate at <=100
users; the scans below are commented where they'd be wrong at larger scale):
  users:  PK user_id (Google sub)
  meals:  PK user_id, SK sk = "{YYYY-MM-DD}#{meal_id}"
  shares: PK share_token
"""
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

import config
import imaging

# AWS handles are created lazily so a missing/broken AWS config degrades at
# request time (logged errors, 5xx on the affected feature) instead of
# crashing boot — only SECRET_KEY is allowed to hard-fail the app.
_dynamodb = None
_s3 = None


def _dynamo():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource(
            'dynamodb',
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        )
    return _dynamodb


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            's3',
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        )
    return _s3


# Table handles as functions so tests can swap in fakes.
def users_table():
    return _dynamo().Table(config.USERS_TABLE)


def meals_table():
    return _dynamo().Table(config.MEALS_TABLE)


def shares_table():
    return _dynamo().Table(config.SHARES_TABLE)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- Table auto-creation -----------------------------------------------------

_TABLE_SPECS = [
    (lambda: config.USERS_TABLE,
     [{'AttributeName': 'user_id', 'KeyType': 'HASH'}],
     [{'AttributeName': 'user_id', 'AttributeType': 'S'}]),
    (lambda: config.MEALS_TABLE,
     [{'AttributeName': 'user_id', 'KeyType': 'HASH'},
      {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
     [{'AttributeName': 'user_id', 'AttributeType': 'S'},
      {'AttributeName': 'sk', 'AttributeType': 'S'}]),
    (lambda: config.SHARES_TABLE,
     [{'AttributeName': 'share_token', 'KeyType': 'HASH'}],
     [{'AttributeName': 'share_token', 'AttributeType': 'S'}]),
]


def ensure_tables():
    """Create any missing table (PAY_PER_REQUEST). A failure logs but never
    crashes boot — the app degrades per-feature instead."""
    for name_fn, key_schema, attr_defs in _TABLE_SPECS:
        name = name_fn()
        try:
            table = _dynamo().Table(name)
            try:
                table.load()
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    raise
                print(f"Creating DynamoDB table {name} ...")
                created = _dynamo().create_table(
                    TableName=name,
                    KeySchema=key_schema,
                    AttributeDefinitions=attr_defs,
                    BillingMode='PAY_PER_REQUEST',
                )
                created.wait_until_exists()
                print(f"Created DynamoDB table {name}")
        except Exception as e:
            print(f"WARNING: could not ensure table {name}: {type(e).__name__}")


# --- Users -------------------------------------------------------------------

def get_user(user_id):
    """Fresh read of one user row, or None."""
    resp = users_table().get_item(Key={'user_id': user_id})
    return resp.get('Item')


def count_users():
    """Total user rows via scan(Select='COUNT'). A scan is fine at <=100 users
    (~one 4KB page) — do NOT 'fix' this with a GSI or counter item at PoC scale."""
    total = 0
    kwargs = {'Select': 'COUNT'}
    while True:
        resp = users_table().scan(**kwargs)
        total += resp.get('Count', 0)
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return total
        kwargs['ExclusiveStartKey'] = lek


def create_user(user_id, email, name, status, picture=''):
    """Create a user row (first sign-in). Returns the item."""
    item = {
        'user_id': user_id,
        'email': email,
        'name': name,
        'picture': picture,
        'status': status,
        'created_at': _utc_now_iso(),
    }
    if status == 'admin':
        item['approved_at'] = item['created_at']
    users_table().put_item(Item=item)
    return item


def update_user_profile(user_id, email, name, picture=''):
    """Keep email/name/picture current on sign-in (they change; sub is stable)."""
    users_table().update_item(
        Key={'user_id': user_id},
        UpdateExpression='SET email = :e, #n = :n, picture = :p',
        ExpressionAttributeNames={'#n': 'name'},
        ExpressionAttributeValues={':e': email, ':n': name, ':p': picture},
    )


def set_user_nutrient(user_id, key, label, unit, goal, direction):
    """Persist the user's tracked-nutrient config (already validated by the
    route). goal 0 is the "follow the catalog default" sentinel — see
    config.resolve_nutrient; label/unit/direction are stored for legacy
    compatibility but the resolver treats the catalog as authoritative for
    catalog keys."""
    users_table().update_item(
        Key={'user_id': user_id},
        UpdateExpression=('SET nutrient_key = :k, nutrient_label = :l, '
                          'nutrient_unit = :u, nutrient_goal = :g, '
                          'nutrient_direction = :d'),
        ExpressionAttributeValues={
            ':k': key, ':l': label, ':u': unit,
            ':g': Decimal(str(goal)), ':d': direction,
        },
    )


def list_users():
    """All user rows. Scan + client-side handling is fine at <=100 users."""
    items = []
    kwargs = {}
    while True:
        resp = users_table().scan(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items
        kwargs['ExclusiveStartKey'] = lek


def set_user_status(user_id, status):
    """Set a user's status (admin approve/reject)."""
    expr = 'SET #s = :s'
    values = {':s': status}
    if status == 'approved':
        expr += ', approved_at = :t'
        values[':t'] = _utc_now_iso()
    users_table().update_item(
        Key={'user_id': user_id},
        UpdateExpression=expr,
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues=values,
    )


def delete_user(user_id):
    users_table().delete_item(Key={'user_id': user_id})


# --- AI daily-use counter (race-safe two-call conditional pattern) -----------

def try_consume_ai_use(user_id, today_str, limit):
    """Consume one AI use for this UTC day. Returns True if allowed.

    Call BEFORE the OpenAI request (refund on upstream failure). Two-call
    pattern so concurrent requests can't blow past the cap:
      1) day reset: claim use #1 if the stored day differs from today;
      2) same day: atomic ADD 1 conditioned on count < limit.
    """
    table = users_table()
    try:
        table.update_item(
            Key={'user_id': user_id},
            UpdateExpression='SET ai_uses_date = :today, ai_uses_today = :one',
            ConditionExpression='attribute_not_exists(ai_uses_date) OR ai_uses_date <> :today',
            ExpressionAttributeValues={':today': today_str, ':one': 1},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
    try:
        table.update_item(
            Key={'user_id': user_id},
            UpdateExpression='ADD ai_uses_today :one',
            ConditionExpression='ai_uses_date = :today AND ai_uses_today < :limit',
            ExpressionAttributeValues={':today': today_str, ':one': 1, ':limit': limit},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def refund_ai_use(user_id, today_str):
    """Best-effort refund after an upstream 5xx/timeout (never raises)."""
    try:
        users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression='ADD ai_uses_today :neg',
            ConditionExpression='ai_uses_date = :today',
            ExpressionAttributeValues={':today': today_str, ':neg': -1},
        )
    except Exception as e:
        print(f"AI-use refund skipped for user {user_id}: {type(e).__name__}")


# --- Meals -------------------------------------------------------------------
# One item per meal: PK user_id, SK "{date}#{meal_id}". meal_id starts with
# HHMMSS (client-provided time) so meals sort chronologically within a day.

def meal_sk(date_str, meal_id):
    return f'{date_str}#{meal_id}'


def _query_all_meals(condition):
    """Run one Query to completion (defensive LastEvaluatedKey pagination),
    ascending by sk => chronological."""
    items = []
    kwargs = {'KeyConditionExpression': condition, 'ScanIndexForward': True}
    while True:
        resp = meals_table().query(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items
        kwargs['ExclusiveStartKey'] = lek


def query_meals_month(user_id, month_str):
    """All of one user's meals in a month (month_str 'YYYY-MM') — ONE Query."""
    return _query_all_meals(
        Key('user_id').eq(user_id) & Key('sk').begins_with(f'{month_str}-'))


def query_meals_range(user_id, start_date_str, end_date_str):
    """All meals with start <= date <= end. '~' sorts after every meal_id
    character, making the end day inclusive."""
    return _query_all_meals(
        Key('user_id').eq(user_id)
        & Key('sk').between(f'{start_date_str}#', f'{end_date_str}#~'))


def query_meals_day(user_id, date_str):
    return _query_all_meals(
        Key('user_id').eq(user_id) & Key('sk').begins_with(f'{date_str}#'))


def get_meal(user_id, date_str, meal_id):
    resp = meals_table().get_item(
        Key={'user_id': user_id, 'sk': meal_sk(date_str, meal_id)})
    return resp.get('Item')


def put_meal(item):
    """item must already carry user_id and sk (built via meal_sk)."""
    meals_table().put_item(Item=item)


def delete_meal_item(user_id, date_str, meal_id):
    meals_table().delete_item(
        Key={'user_id': user_id, 'sk': meal_sk(date_str, meal_id)})


def delete_all_meals(user_id):
    """Delete every meal row for a user (account deletion)."""
    items = _query_all_meals(Key('user_id').eq(user_id))
    with meals_table().batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={'user_id': user_id, 'sk': item['sk']})
    return len(items)


# --- Shares ------------------------------------------------------------------

def share_is_active(row, now=None):
    """True when a share row is neither revoked nor expired.
    expires_at absent = never expires."""
    if not row or row.get('revoked'):
        return False
    expires_at = row.get('expires_at')
    return expires_at is None or int(expires_at) > (now or time.time())


def get_share(token):
    resp = shares_table().get_item(Key={'share_token': token})
    return resp.get('Item')


def list_user_shares(user_id):
    """One user's share rows via filtered scan — deliberate at PoC scale
    (<=100 users x <=20 links); do not add a GSI for this."""
    items = []
    kwargs = {'FilterExpression': Attr('user_id').eq(user_id)}
    while True:
        resp = shares_table().scan(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items
        kwargs['ExclusiveStartKey'] = lek


def create_share(user_id, label=None, expires_at=None):
    """Create a share link row. Token is 192 bits — unguessable."""
    item = {
        'share_token': secrets.token_urlsafe(24),
        'user_id': user_id,
        'created_at': _utc_now_iso(),
        'revoked': False,
    }
    if label:
        item['label'] = label
    if expires_at is not None:
        item['expires_at'] = int(expires_at)
    shares_table().put_item(Item=item)
    return item


def revoke_share(token, user_id):
    """Revoke a share only if it belongs to user_id (atomic ownership check).
    Returns True on success, False if missing or not owned. Rows are kept."""
    try:
        shares_table().update_item(
            Key={'share_token': token},
            UpdateExpression='SET revoked = :r',
            ConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':r': True, ':uid': user_id},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def delete_user_shares(user_id):
    """Delete all of a user's share rows (account deletion)."""
    rows = list_user_shares(user_id)
    with shares_table().batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={'share_token': row['share_token']})
    return len(rows)


# --- S3 photos ---------------------------------------------------------------
# Keys are built HERE ONLY, from the resolved user_id + validated date +
# server-generated meal_id — never from client input.

def photo_key(user_id, date_str, meal_id):
    return f'users/{user_id}/meals/{date_str}/{meal_id}.jpg'


def put_photo(file_storage, key):
    """Normalize any uploaded image to JPEG server-side, then store it.

    The client downscales to JPEG when it can, but browsers that can't decode
    HEIC upload the raw file — so normalizing here (imaging.to_jpeg) is what
    guarantees a valid JPEG is stored. Raises ValueError on an unreadable image.
    """
    jpeg_bytes = imaging.to_jpeg(file_storage.read())
    _s3_client().put_object(
        Bucket=config.S3_BUCKET,
        Key=key,
        Body=jpeg_bytes,
        ContentType='image/jpeg',
    )


def delete_photo(key):
    """Best-effort delete (never raises)."""
    if not key or not config.S3_BUCKET:
        return
    try:
        _s3_client().delete_object(Bucket=config.S3_BUCKET, Key=key)
    except Exception as e:
        print(f"Error deleting photo object: {type(e).__name__}")


def presign_photo(key, owner_user_id):
    """Presigned GET URL for a photo, or None.

    Defense in depth: refuses to sign any key outside the resolved owner's
    users/{user_id}/ prefix, even if a bad key somehow reached the table.
    """
    if not key or not config.S3_BUCKET:
        return None
    if not key.startswith(f'users/{owner_user_id}/'):
        print(f"REFUSED to presign key outside user prefix (user {owner_user_id})")
        return None
    try:
        return _s3_client().generate_presigned_url(
            'get_object',
            Params={'Bucket': config.S3_BUCKET, 'Key': key},
            ExpiresIn=config.PHOTO_URL_TTL,
        )
    except Exception as e:
        print(f"Error presigning photo URL: {type(e).__name__}")
        return None


def delete_user_photos(user_id):
    """Delete the whole users/{user_id}/ S3 prefix (account deletion).

    Raises on any failure — the caller must NOT delete the user row unless this
    completes, or private photos would be orphaned with no account to retry the
    wipe. delete_objects reports per-object errors even on a 200, so those are
    surfaced too.
    """
    if not config.S3_BUCKET:
        return 0
    deleted = 0
    prefix = f'users/{user_id}/'
    kwargs = {'Bucket': config.S3_BUCKET, 'Prefix': prefix}
    while True:
        resp = _s3_client().list_objects_v2(**kwargs)
        objs = [{'Key': o['Key']} for o in resp.get('Contents', [])]
        if objs:
            result = _s3_client().delete_objects(
                Bucket=config.S3_BUCKET, Delete={'Objects': objs, 'Quiet': True})
            errors = result.get('Errors') or []
            if errors:
                raise RuntimeError(f'{len(errors)} S3 object(s) failed to delete')
            deleted += len(objs)
        if not resp.get('IsTruncated'):
            break
        kwargs['ContinuationToken'] = resp['NextContinuationToken']
    return deleted
