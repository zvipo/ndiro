"""DynamoDB + S3 access for Ndiro.

Multi-tenant discipline: every meal/share accessor takes an explicit user_id
that callers must resolve from the session (never from URL/query/form input),
and S3 keys are constructed here, server-side only, under users/{user_id}/.

Tables (all on-demand, auto-created on boot, no GSIs — deliberate at <=100
users; the scans below are commented where they'd be wrong at larger scale):
  users:  PK user_id (Google sub, or 'nat-' + HMAC-SHA256(secret, email)[:32]
          for native accounts — deterministic AND keyed, see
          native_auth.new_user_id)
  meals:  PK user_id, SK sk = "{YYYY-MM-DD}#{meal_id}"
  shares: PK share_token
"""
import secrets
import threading
import time
from collections import OrderedDict
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


def invites_table():
    return _dynamo().Table(config.INVITES_TABLE)


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
    (lambda: config.INVITES_TABLE,
     [{'AttributeName': 'invite_token', 'KeyType': 'HASH'}],
     [{'AttributeName': 'invite_token', 'AttributeType': 'S'}]),
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


def create_user(user_id, email, name, status, picture='', invited_by=None):
    """Create a user row (first sign-in). Returns the item.
    invited_by = inviter user_id when an invite auto-approved this signup."""
    item = {
        'user_id': user_id,
        'email': email,
        'name': name,
        'picture': picture,
        'status': status,
        'created_at': _utc_now_iso(),
    }
    if status in ('admin', 'approved'):
        item['approved_at'] = item['created_at']
    if invited_by:
        item['invited_by'] = invited_by
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


def list_users(consistent=False):
    """All user rows. Scan + client-side handling is fine at <=100 users.
    consistent=True for credential/account-state lookups (sign-in, token
    resolution, signup duplicate checks): the default eventually-consistent
    scan could serve a pre-lockout counter, a pre-change password hash, or
    miss a token written moments ago. Admin/stats listings stay eventual."""
    items = []
    kwargs = {'ConsistentRead': True} if consistent else {}
    while True:
        resp = users_table().scan(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items
        kwargs['ExclusiveStartKey'] = lek


def approve_pending_user(user_id, invited_by):
    """Approve a user ONLY if they are still pending (conditional write):
    an invite redemption must never overwrite a concurrent admin rejection —
    rejection is a ban and must stick. True on success, False on the race."""
    try:
        users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression='SET #s = :s, approved_at = :t, invited_by = :i',
            ConditionExpression='#s = :p',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'approved', ':p': 'pending',
                                       ':t': _utc_now_iso(), ':i': invited_by},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def set_user_status(user_id, status):
    """Set a user's status (admin approve/reject). Conditional on the row
    still existing: an admin action racing account deletion must not upsert
    a partial {user_id, status} row. True unless the row was gone."""
    expr = 'SET #s = :s'
    values = {':s': status}
    if status == 'approved':
        expr += ', approved_at = :t'
        values[':t'] = _utc_now_iso()
    try:
        users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression=expr,
            ConditionExpression='attribute_exists(user_id)',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def delete_user(user_id):
    users_table().delete_item(Key={'user_id': user_id})


# --- Native (email/password) accounts ----------------------------------------
# All native-account state lives on the ONE users row (auth_provider,
# password_hash, email_verified, hashed single-use tokens, lockout counters),
# so account deletion and the admin/monitor scans need no extra steps. Email
# and token lookups are full scans — fine at <=100 users, same as list_users.

def find_user_by_email(email, provider=None):
    """First user row whose email matches (case-insensitive), or None.
    provider='native' additionally requires auth_provider == 'native'."""
    email = (email or '').strip().lower()
    if not email:
        return None
    for item in list_users(consistent=True):
        if (item.get('email') or '').lower() != email:
            continue
        if provider == 'native' and item.get('auth_provider') != 'native':
            continue
        return item
    return None


def delete_stale_native_signup(user_id, verify_expires_at):
    """Conditional delete for signup's stale-row purge: only removes a row
    that is STILL a native, unverified, PENDING signup carrying the exact
    expired verify_expires_at the caller's scan observed. A resend that
    refreshed the token, a rejection, or a completed verification landing
    in between wins the race — the row stays. True when deleted."""
    try:
        users_table().delete_item(
            Key={'user_id': user_id},
            ConditionExpression=('auth_provider = :n AND email_verified = :f '
                                 'AND #s = :p AND verify_expires_at = :e'),
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':n': 'native', ':f': False,
                                       ':p': 'pending',
                                       ':e': int(verify_expires_at)},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def find_user_by_token_hash(attr_name, token_hash):
    """User row carrying this verify/reset token HASH, or None. Only hashes
    are ever stored, so a scan can never surface a live link."""
    if not token_hash:
        return None
    for item in list_users(consistent=True):
        if item.get(attr_name) == token_hash:
            return item
    return None


def create_native_user(user_id, email, name, password_hash,
                       verify_token_hash, verify_expires_at,
                       pending_invite_token=None):
    """Create an unverified native-account row, or None if the row already
    exists. The put is CONDITIONAL on the key being free and the user_id is
    derived from the email (native_auth.new_user_id), so two concurrent
    signups for one address race on the same key and exactly one wins —
    email uniqueness without a GSI. Always status 'pending': invites are
    validated at signup but CLAIMED only at verification (see app.py), and
    ADMIN_EMAILS never bootstraps a native account."""
    item = {
        'user_id': user_id,
        'email': email,
        'name': name,
        'picture': '',
        'status': 'pending',
        'created_at': _utc_now_iso(),
        'auth_provider': 'native',
        'password_hash': password_hash,
        'email_verified': False,
        'verify_token_hash': verify_token_hash,
        'verify_expires_at': int(verify_expires_at),
    }
    if pending_invite_token:
        item['pending_invite_token'] = pending_invite_token
    try:
        users_table().put_item(
            Item=item, ConditionExpression='attribute_not_exists(user_id)')
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return None
    return item


def mark_email_verified(user_id, token_hash):
    """Flip email_verified and clear the token — conditional on the row still
    carrying THIS token hash, so a verification link is single-use even under
    a double-submit race. True on success, False when the condition failed."""
    try:
        users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression=('SET email_verified = :t '
                              'REMOVE verify_token_hash, verify_expires_at, '
                              'pending_invite_token'),
            ConditionExpression='verify_token_hash = :h',
            ExpressionAttributeValues={':t': True, ':h': token_hash},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def _update_if_exists(user_id, update_expression, values=None):
    """update_item conditioned on the row still existing. DynamoDB updates
    UPSERT by default, so a write racing account deletion would otherwise
    resurrect a partial user row — deletion must be final. Racers that lose
    (row gone) are silently dropped; True when the write landed."""
    kwargs = {
        'Key': {'user_id': user_id},
        'UpdateExpression': update_expression,
        'ConditionExpression': 'attribute_exists(user_id)',
    }
    if values:
        kwargs['ExpressionAttributeValues'] = values
    try:
        users_table().update_item(**kwargs)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def set_verify_token(user_id, token_hash, expires_at):
    """Re-mint the verification token (resend flow)."""
    _update_if_exists(
        user_id, 'SET verify_token_hash = :h, verify_expires_at = :e',
        {':h': token_hash, ':e': int(expires_at)})


def set_reset_token(user_id, token_hash, expires_at):
    _update_if_exists(
        user_id, 'SET reset_token_hash = :h, reset_expires_at = :e',
        {':h': token_hash, ':e': int(expires_at)})


def complete_password_reset(user_id, password_hash, token_hash):
    """Set the new password and clear the reset token + lockout in one
    conditional write — the token-hash condition makes the link single-use.
    True on success, False when the condition failed (used/replaced token)."""
    try:
        users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression=('SET password_hash = :p '
                              'REMOVE reset_token_hash, reset_expires_at, '
                              'failed_logins, locked_until'),
            ConditionExpression='reset_token_hash = :h',
            ExpressionAttributeValues={':p': password_hash, ':h': token_hash},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def set_password_hash(user_id, password_hash, expected_hash):
    """Signed-in password change (settings): a compare-and-set on the exact
    hash the route just verified, so a stale request that validated an old
    password can never overwrite a newer change or reset that committed in
    between. Also clears any outstanding reset token in the same atomic
    write — an old reset email must not be able to overwrite the freshly
    chosen password. True on success, False when the condition lost."""
    try:
        users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression=('SET password_hash = :p '
                              'REMOVE reset_token_hash, reset_expires_at'),
            ConditionExpression='password_hash = :old',
            ExpressionAttributeValues={':p': password_hash,
                                       ':old': expected_hash},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def record_login_failure(user_id, threshold, lockout_seconds):
    """Atomically bump the consecutive-failure counter and lock the account
    once it reaches threshold. ADD is DynamoDB's atomic increment, so
    concurrent wrong guesses can never lose an update and slip past the
    lockout; the lock decision uses the count THIS write produced."""
    try:
        resp = users_table().update_item(
            Key={'user_id': user_id},
            UpdateExpression='ADD failed_logins :one',
            ConditionExpression='attribute_exists(user_id)',
            ExpressionAttributeValues={':one': 1},
            ReturnValues='ALL_NEW',
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return  # row deleted mid-race: nothing to count against
    count = int(resp.get('Attributes', {}).get('failed_logins') or 0)
    if count >= threshold:
        # Conditioned on the counter STILL being at threshold: a successful
        # login landing between the increment and this write clears the
        # counter, and must not be re-locked by a stale loser.
        try:
            users_table().update_item(
                Key={'user_id': user_id},
                UpdateExpression='SET locked_until = :t',
                ConditionExpression=('attribute_exists(user_id) '
                                     'AND failed_logins >= :n'),
                ExpressionAttributeValues={
                    ':t': int(time.time()) + int(lockout_seconds),
                    ':n': int(threshold)},
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise


def clear_login_failures(user_id):
    _update_if_exists(user_id, 'REMOVE failed_logins, locked_until')


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


# --- Invites -----------------------------------------------------------------
# Single-use, expiring, auto-approving signup links. Mirrors the shares
# machinery, but FAIL-CLOSED: invites grant privilege, so a row with no
# expires_at counts as inactive (shares treat that as "never expires").

def invite_is_active(row, now=None):
    """True when an invite is unrevoked, unused, and unexpired."""
    if not row or row.get('revoked') or 'used_by' in row:
        return False
    expires_at = row.get('expires_at')
    return expires_at is not None and int(expires_at) > (now or time.time())


def get_invite(token):
    resp = invites_table().get_item(Key={'invite_token': token})
    return resp.get('Item')


def list_user_invites(user_id):
    """One user's invite rows via filtered scan — deliberate at PoC scale
    (<=100 users x <=10 links); do not add a GSI for this."""
    items = []
    kwargs = {'FilterExpression': Attr('user_id').eq(user_id)}
    while True:
        resp = invites_table().scan(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items
        kwargs['ExclusiveStartKey'] = lek


def create_invite(user_id, expires_at, label=None):
    """Create an invite row. Token is 192 bits — unguessable; expiry required."""
    item = {
        'invite_token': secrets.token_urlsafe(24),
        'user_id': user_id,
        'created_at': _utc_now_iso(),
        'expires_at': int(expires_at),
        'revoked': False,
    }
    if label:
        item['label'] = label
    invites_table().put_item(Item=item)
    return item


def claim_invite(token, new_user_id):
    """Atomically consume a single-use invite for new_user_id.

    Condition (DynamoDB precedence: AND binds tighter than OR):
      (exists AND unused AND unrevoked AND unexpired) OR used_by = :u
    The first arm makes the claim race-safe AND fully in-condition — a revoke
    or expiry landing after the caller's validation read still loses. The OR
    arm makes the claim IDEMPOTENT for the same claimant: a signup that
    claimed but crashed before its account write retries through the same
    link instead of burning it. Different claimants race only on the first
    arm — exactly one ever wins."""
    try:
        invites_table().update_item(
            Key={'invite_token': token},
            UpdateExpression='SET used_by = :u, used_at = :t',
            ConditionExpression=(
                'attribute_exists(invite_token) AND attribute_not_exists(used_by) '
                'AND revoked = :f AND expires_at > :now OR used_by = :u'),
            ExpressionAttributeValues={
                ':u': new_user_id, ':t': _utc_now_iso(),
                ':f': False, ':now': int(time.time()),
            },
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def revoke_invite(token, user_id):
    """Revoke an invite only if it belongs to user_id AND is still unused
    (atomic): "revoking" an already-redeemed invite would tell the inviter
    they stopped something that already admitted someone. Returns True on
    success, False if missing, not owned, or used. Rows are kept."""
    try:
        invites_table().update_item(
            Key={'invite_token': token},
            UpdateExpression='SET revoked = :r',
            ConditionExpression='user_id = :uid AND attribute_not_exists(used_by)',
            ExpressionAttributeValues={':r': True, ':uid': user_id},
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def delete_user_invites(user_id):
    """Delete all of a user's invite rows (account deletion)."""
    rows = list_user_invites(user_id)
    with invites_table().batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={'invite_token': row['invite_token']})
    return len(rows)


# --- S3 photos ---------------------------------------------------------------
# Keys are built HERE ONLY, from the resolved user_id + validated date +
# server-generated meal_id — never from client input.

class _PhotoCache:
    """Byte-budgeted LRU for proxied photo bytes, keyed (s3_key, version).

    Valid because gunicorn runs ONE worker (same rationale as the memory://
    rate limiter); the lock covers the 8 request threads. It guards only the
    dict/size bookkeeping — never held across S3 I/O. Versioned keys mean a
    photo replace (same S3 key, new meal updated_at) misses naturally; stale
    versions are dropped on put or age out via LRU. No stats and no logging:
    photo activity is meal data (invariant #8)."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self._items = OrderedDict()  # (s3_key, version) -> bytes
        self._size = 0
        self._lock = threading.Lock()

    def get(self, s3_key, version):
        with self._lock:
            data = self._items.get((s3_key, version))
            if data is not None:
                self._items.move_to_end((s3_key, version))
            return data

    def put(self, s3_key, version, data):
        if len(data) > self.max_bytes:
            return  # never let one object own the whole budget
        with self._lock:
            # Older versions of the same key are dead weight — drop them.
            for k in [k for k in self._items if k[0] == s3_key and k[1] != version]:
                self._size -= len(self._items.pop(k))
            old = self._items.pop((s3_key, version), None)
            if old is not None:
                self._size -= len(old)
            self._items[(s3_key, version)] = data
            self._size += len(data)
            while self._size > self.max_bytes and self._items:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)

    def drop_prefix(self, prefix):
        """Drop every entry whose s3_key starts with prefix (exact key or a
        whole users/{user_id}/ subtree — deleted photos must not linger)."""
        with self._lock:
            for k in [k for k in self._items if k[0].startswith(prefix)]:
                self._size -= len(self._items.pop(k))


_photo_cache = _PhotoCache(config.PHOTO_CACHE_MB * 1024 * 1024)


def photo_key(user_id, date_str, meal_id):
    return f'users/{user_id}/meals/{date_str}/{meal_id}.jpg'


def get_photo_bytes(key, owner_user_id, version):
    """Photo bytes via the LRU cache, or None when absent/refused.

    version MUST be the app's _photo_version for the owning meal — it is the
    cache key's second half, and a caller inventing its own (or passing '')
    would cache bytes no photo replace ever invalidates. Deliberately no
    default: every new caller has to make that decision consciously.

    Defense in depth: refuses any key outside the resolved owner's
    users/{user_id}/ prefix, even if a bad key somehow reached a caller.
    Missing S3 object -> None; infrastructure failures RAISE so routes can
    answer 502 instead of a misleading 404."""
    if not key or not config.S3_BUCKET:
        return None
    if not key.startswith(f'users/{owner_user_id}/'):
        print(f"REFUSED photo read outside user prefix (user {owner_user_id})")
        return None
    data = _photo_cache.get(key, version)
    if data is not None:
        return data
    try:
        resp = _s3_client().get_object(Bucket=config.S3_BUCKET, Key=key)
    except ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', '404'):
            return None
        raise
    data = resp['Body'].read()
    _photo_cache.put(key, version, data)
    return data


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
    # Every byte-mutation site must touch the LRU: if the caller's row write
    # fails after this overwrite, the old version would otherwise keep
    # serving the OLD bytes from cache while S3 holds the new ones.
    _photo_cache.drop_prefix(key)


def delete_photo(key):
    """Best-effort delete (never raises).

    Cache purge AFTER the S3 delete: purging first would let an in-flight
    get_photo_bytes (no lock held across S3 I/O) repopulate the LRU with the
    just-deleted bytes."""
    if not key or not config.S3_BUCKET:
        return
    try:
        _s3_client().delete_object(Bucket=config.S3_BUCKET, Key=key)
    except Exception as e:
        print(f"Error deleting photo object: {type(e).__name__}")
    finally:
        _photo_cache.drop_prefix(key)


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
    try:
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
    finally:
        # AFTER the S3 wipe (even a partial one): purging first would let an
        # in-flight read repopulate the LRU with a deleted user's bytes,
        # which nothing would ever purge again.
        _photo_cache.drop_prefix(prefix)
    return deleted


# --- Instance stats (admin monitoring) ---------------------------------------
# Full-table scans, deliberately: at PoC scale (<=100 users) every table is a
# handful of pages, and the alternative — GSIs or counter items kept in sync on
# every write — is far more machinery to get wrong. The page caps below keep one
# refresh bounded so a runaway table can never hold the single gunicorn worker
# past its 60s timeout; a capped run reports truncated=True rather than lying.

_STATS_SCAN_PAGE_CAP = 40   # DynamoDB scan pages (~1 MB each) per table
_STATS_LIST_PAGE_CAP = 50   # S3 listing pages (1000 keys each) => 50k objects


def _scan_all_pages(table, **kwargs):
    """Scan up to _STATS_SCAN_PAGE_CAP pages. Returns (items, truncated).

    The cap is read from the module at call time, not bound as a default —
    tests turn it down to prove a runaway table stops instead of spinning."""
    items = []
    pages = 0
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        pages += 1
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items, False
        if pages >= _STATS_SCAN_PAGE_CAP:
            return items, True
        kwargs['ExclusiveStartKey'] = lek


def scan_meal_stats():
    """Per-user meal counts and the first/last logged date, from ONE scan.

    The ProjectionExpression is load-bearing SECURITY, not an optimization:
    only the partition key and the date come back, so meal descriptions,
    contexts, and nutrient values never enter the process at all — the
    monitoring path structurally cannot see another user's meals (invariant #7).

    Returns {'total', 'truncated', 'by_date': {date: count}, 'per_user':
    {user_id: {'meals', 'days', 'first', 'last'}}} where days counts distinct
    logged dates. Dates are the CLIENT's local dates (invariant #10), so any
    window built from them is the users' own day, not the server's.

    per_user exists so the caller can compute CARDINALITIES (how many accounts
    have logged, how many logged this week) and spot rows belonging to no
    account. It is an intermediate, not an output: nothing keyed by user_id may
    be serialized to an admin — see the note above /api/admin/stats in app.py.
    """
    items, truncated = _scan_all_pages(
        meals_table(),
        ProjectionExpression='user_id, #d',
        ExpressionAttributeNames={'#d': 'date'},
    )
    per_user = {}
    by_date = {}
    for item in items:
        user_id = item.get('user_id')
        if not user_id:
            continue
        row = per_user.setdefault(user_id, {'meals': 0, 'dates': set()})
        row['meals'] += 1
        date_str = item.get('date')
        if date_str:
            row['dates'].add(date_str)
            by_date[date_str] = by_date.get(date_str, 0) + 1
    out = {}
    for user_id, row in per_user.items():
        dates = row.pop('dates')
        out[user_id] = {
            'meals': row['meals'],
            'days': len(dates),
            'first': min(dates) if dates else None,
            'last': max(dates) if dates else None,
        }
    return {'total': len(items), 'truncated': truncated, 'per_user': out,
            'by_date': by_date}


def scan_photo_stats():
    """Per-user photo object counts and bytes from the S3 listing.

    Keys and sizes only — no object is ever fetched, so photo bytes never
    enter the process (invariant #7). Returns enabled=False when photo storage
    is not configured, which is a normal state, not an error.

    As with scan_meal_stats, per_user is an intermediate for totals and orphan
    detection — never something an admin is shown.
    """
    if not config.S3_BUCKET:
        return {'enabled': False, 'total': 0, 'bytes': 0, 'truncated': False,
                'per_user': {}}
    per_user = {}
    total = 0
    total_bytes = 0
    truncated = False
    pages = 0
    kwargs = {'Bucket': config.S3_BUCKET, 'Prefix': 'users/'}
    while True:
        resp = _s3_client().list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            # Keys are built server-side as users/{user_id}/meals/{date}/{id}.jpg
            parts = obj['Key'].split('/')
            if len(parts) < 2 or not parts[1]:
                continue
            size = int(obj.get('Size', 0) or 0)
            row = per_user.setdefault(parts[1], {'photos': 0, 'bytes': 0})
            row['photos'] += 1
            row['bytes'] += size
            total += 1
            total_bytes += size
        pages += 1
        if not resp.get('IsTruncated'):
            break
        if pages >= _STATS_LIST_PAGE_CAP:
            truncated = True
            break
        kwargs['ContinuationToken'] = resp['NextContinuationToken']
    return {'enabled': True, 'total': total, 'bytes': total_bytes,
            'truncated': truncated, 'per_user': per_user}


def scan_share_stats(now=None):
    """Share rows bucketed into MUTUALLY EXCLUSIVE outcomes that sum to total:
    active -> revoked -> expired. Rows are kept after revoke/expiry by design,
    so 'total' is lifetime links minted, not live ones.

    Totals only — deliberately not grouped by user_id. Who holds a live share
    link is between that user and their /shares page."""
    rows, truncated = _scan_all_pages(shares_table())
    now = now or time.time()
    stats = {'total': len(rows), 'active': 0, 'revoked': 0, 'expired': 0,
             'truncated': truncated}
    for row in rows:
        if share_is_active(row, now):
            stats['active'] += 1
        elif row.get('revoked'):
            stats['revoked'] += 1
        else:
            stats['expired'] += 1
    return stats


def scan_invite_stats(now=None):
    """Invite rows bucketed into MUTUALLY EXCLUSIVE outcomes that sum to
    total: used -> revoked -> expired -> open (still redeemable). 'open' is
    the count MAX_ACTIVE_INVITES caps, minus the used ones a cap-time
    invite_is_active() would still include.

    Totals only — like shares, deliberately not grouped by user_id."""
    rows, truncated = _scan_all_pages(invites_table())
    now = now or time.time()
    stats = {'total': len(rows), 'open': 0, 'used': 0, 'revoked': 0,
             'expired': 0, 'truncated': truncated}
    for row in rows:
        if row.get('used_by'):
            stats['used'] += 1
        elif row.get('revoked'):
            stats['revoked'] += 1
        elif not invite_is_active(row, now):
            stats['expired'] += 1
        else:
            stats['open'] += 1
    return stats
