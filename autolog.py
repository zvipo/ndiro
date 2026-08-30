"""Async auto-log: batch photos spooled on LOCAL DISK, processed in background.

The "auto-add from photos" upload returns as soon as the (already normalized)
JPEG lands in the spool directory (AUTOLOG_DIR); a single in-process daemon
thread then runs the AI estimate and commits the meal (photo to S3, row to
DynamoDB) with the uploader long gone. The spool directory IS the queue — one
.jpg plus one .json sidecar (user_id/date/time/attempts, NEVER any meal
content) per pending photo: no table, no extra metadata store. A container
restart merely pauses work if AUTOLOG_DIR is on a volume; on ephemeral disk it
loses only the not-yet-committed photos — an accepted tradeoff, disclosed in
env_template.txt.

Single-worker assumptions hold here exactly like the memory:// rate limiter
and the photo LRU: ONE background thread. It starts lazily on first use
(ensure_worker) because gunicorn runs --preload — a thread started at import
in the master would not survive the fork into the worker process.

Tenant/privacy discipline mirrors app.py:
- user_id enters a sidecar only from the session (the route passes it), and is
  re-read FRESH at processing time — an account rejected or deleted in between
  has its spooled photos discarded, never logged (invariant #3's spirit).
- The AI daily cap is consumed per photo exactly like the interactive
  estimator (consume BEFORE the call, refund on upstream failure). A user at
  the cap still gets the meal — just without an estimate — because silently
  holding food photos until midnight UTC would look like data loss.
- Log lines carry entry ids, user ids, and error types only (invariant #8);
  AI failures go through ai.log_failure like every other estimate.
"""
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import ai
import auth
import config
import db

# Estimate is tried on the first ESTIMATE_ATTEMPTS failures (retried with
# backoff — an OpenAI outage should not permanently strip a meal of its
# estimate); after that the meal is saved WITHOUT one. A failing SAVE keeps
# retrying until MAX_ATTEMPTS, then the entry is dead-lettered (renamed
# *.json.dead, photo kept) so the loop never spins on a poison entry.
ESTIMATE_ATTEMPTS = 3
MAX_ATTEMPTS = 6
RETRY_BASE_S = 300  # attempt N retries after N * RETRY_BASE_S (tests set 0)
POLL_S = 5          # idle scan interval; enqueue wakes the worker immediately

_ENTRY_RE = re.compile(r'^\d+-[0-9a-f]{6}$')

_wake = threading.Event()
_worker_lock = threading.Lock()
_worker = None
WORKER_ENABLED = True  # tests set False and call process_once() themselves


def spool_dir():
    os.makedirs(config.AUTOLOG_DIR, exist_ok=True)
    return config.AUTOLOG_DIR


def _paths(entry_id):
    base = os.path.join(spool_dir(), entry_id)
    return base + '.json', base + '.jpg'


def _list_entries():
    """Pending entry ids, FIFO (the id's epoch-ms prefix sorts them)."""
    try:
        names = os.listdir(spool_dir())
    except OSError:
        return []
    return sorted(n[:-5] for n in names
                  if n.endswith('.json') and _ENTRY_RE.match(n[:-5]))


def pending_count(user_id):
    """How many spooled photos this user is waiting on (dead letters excluded)."""
    n = 0
    for entry_id in _list_entries():
        meta = _read_meta(entry_id)
        if meta and meta.get('user_id') == user_id:
            n += 1
    return n


def _read_meta(entry_id):
    try:
        with open(_paths(entry_id)[0], encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def has_pending(user_id, date_str, time_str):
    """Is a photo already queued for this user at this exact date+time?
    Half of the upload-time dedup (see app.auto_log) — the other half checks
    committed meals. Dead letters don't count: their photo never became a
    meal, so a re-upload of one should be accepted."""
    for entry_id in _list_entries():
        meta = _read_meta(entry_id)
        if meta and meta.get('user_id') == user_id and \
                meta.get('date') == date_str and meta.get('time') == time_str:
            return True
    return False


def enqueue(user_id, date_str, time_str, jpeg_bytes):
    """Spool one photo for background processing. The .jpg is written first,
    the .json sidecar last — the scanner keys on sidecars, so a crash between
    the two leaves an invisible orphan .jpg, never a half-readable entry."""
    entry_id = f'{int(time.time() * 1000)}-{secrets.token_hex(3)}'
    meta_path, jpg_path = _paths(entry_id)
    with open(jpg_path, 'wb') as fh:
        fh.write(jpeg_bytes)
    meta = {'user_id': user_id, 'date': date_str, 'time': time_str,
            'uploaded_at': datetime.now(timezone.utc).isoformat(),
            'attempts': 0}
    with open(meta_path, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh)
    _wake.set()
    return entry_id


def _drop(entry_id):
    for path in _paths(entry_id):
        try:
            os.remove(path)
        except OSError:
            pass


def drop_user(user_id):
    """Purge a user's spool entries (account deletion — photos are private
    data and must not outlive the account). Dead letters included. Raises on
    a failed remove, like db.delete_user_photos: the caller must not delete
    the user row while photos linger."""
    for name in os.listdir(spool_dir()):
        if not (name.endswith('.json') or name.endswith('.json.dead')):
            continue
        path = os.path.join(config.AUTOLOG_DIR, name)
        try:
            with open(path, encoding='utf-8') as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if meta.get('user_id') != user_id:
            continue
        base = name[:-len('.json.dead')] if name.endswith('.dead') else name[:-5]
        os.remove(path)
        jpg = os.path.join(config.AUTOLOG_DIR, base + '.jpg')
        if os.path.exists(jpg):
            os.remove(jpg)


def _defer(entry_id, meta):
    """Record a failed attempt: bump the counter, schedule the retry, and
    dead-letter the entry once MAX_ATTEMPTS is spent."""
    meta['attempts'] = int(meta.get('attempts', 0)) + 1
    if meta['attempts'] >= MAX_ATTEMPTS:
        meta_path = _paths(entry_id)[0]
        try:
            os.replace(meta_path, meta_path + '.dead')
        except OSError:
            pass
        print(f"AUTOLOG dead-letter {entry_id} for user {meta.get('user_id')} "
              f"after {meta['attempts']} attempts")
        return
    meta['next_try'] = time.time() + meta['attempts'] * RETRY_BASE_S
    try:
        with open(_paths(entry_id)[0], 'w', encoding='utf-8') as fh:
            json.dump(meta, fh)
    except OSError:
        pass


def _process_entry(entry_id):
    """One spooled photo -> one meal. Returns True when the entry is finished
    (committed or discarded), False when it was deferred for retry."""
    meta = _read_meta(entry_id)
    meta_path, jpg_path = _paths(entry_id)
    if meta is None:
        return True  # racing drop/dead-letter
    if float(meta.get('next_try', 0)) > time.time():
        return False
    user_id = meta.get('user_id')
    date_str, time_str = meta.get('date'), meta.get('time')
    try:
        with open(jpg_path, 'rb') as fh:
            jpeg_bytes = fh.read()
    except OSError:
        print(f"AUTOLOG {entry_id}: photo file missing — dropping sidecar")
        _drop(entry_id)
        return True

    # Fresh account read (like the request decorators): a rejected or deleted
    # account's spooled photos are discarded, never logged.
    try:
        user = db.get_user(user_id)
    except Exception as e:
        print(f"AUTOLOG {entry_id}: user read failed: {type(e).__name__}")
        _defer(entry_id, meta)
        return False
    if not user or user.get('status') not in auth.APPROVED_STATUSES:
        print(f"AUTOLOG {entry_id}: user {user_id} no longer approved — discarding")
        _drop(entry_id)
        return True

    cfg = config.resolve_nutrient(user)
    # A commit retry reuses the estimate cached on the sidecar by an earlier
    # attempt (see below) instead of consuming another AI use. The cached
    # description sits on the same private local disk as the photo itself.
    est = meta.get('est')
    if est is None and config.OPENAI_API_KEY \
            and int(meta.get('attempts', 0)) < ESTIMATE_ATTEMPTS:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        try:
            allowed = db.try_consume_ai_use(user_id, today, config.AI_DAILY_LIMIT)
        except Exception as e:
            ai.log_failure('cap', {'route': 'auto-log', 'user': user_id,
                                   'error': type(e).__name__, 'detail': str(e)})
            _defer(entry_id, meta)
            return False
        if allowed:
            result, err = ai.estimate_photo(jpeg_bytes, cfg,
                                            log_context={'user': user_id,
                                                         'route': 'auto-log'})
            if err:
                _message, _status, refundable, _ref = err
                if refundable:
                    db.refund_ai_use(user_id, today)  # best-effort
                _defer(entry_id, meta)  # retried; falls back to no-estimate
                return False
            est = {'description': result.get('description') or '',
                   'amount': result['amount']}
        # Cap reached: fall through and save without an estimate — holding the
        # meal until the UTC day rolls over would look like a lost upload.

    now_iso = datetime.now(timezone.utc).isoformat()
    meal_id = f"{time_str.replace(':', '')}00-{secrets.token_hex(3)}"
    item = {
        'user_id': user_id,
        'sk': db.meal_sk(date_str, meal_id),
        'date': date_str,
        'meal_id': meal_id,
        'description': (est and est.get('description'))
        or 'Photo meal — description pending',
        'nutrients': {},
        'created_at': now_iso,
        'updated_at': now_iso,
    }
    if est:
        item['ai_assisted'] = True
        item['nutrients'][cfg['key']] = Decimal(str(est['amount']))

    photo_key = None
    if config.S3_BUCKET:
        photo_key = db.photo_key(user_id, date_str, meal_id)
        item['photo_key'] = photo_key
        item['photo_v'] = now_iso
    try:
        if photo_key:
            db.put_photo(io.BytesIO(jpeg_bytes), photo_key)
        db.put_meal(item)
    except Exception as e:
        print(f"AUTOLOG {entry_id}: commit failed for user {user_id}: "
              f"{type(e).__name__}")
        if photo_key:
            db.delete_photo(photo_key)  # don't orphan the just-uploaded object
        if est:
            meta['est'] = est  # keep the paid-for estimate for the retry
        _defer(entry_id, meta)
        return False

    _drop(entry_id)
    return True


def process_once():
    """One pass over the spool. Returns how many entries were finished.
    Exceptions per entry never kill the pass — one poison photo must not
    block everyone else's queue."""
    done = 0
    for entry_id in _list_entries():
        try:
            if _process_entry(entry_id):
                done += 1
        except Exception as e:
            print(f"AUTOLOG {entry_id}: unexpected {type(e).__name__}")
            meta = _read_meta(entry_id)
            if meta is not None:
                _defer(entry_id, meta)
    return done


def _worker_loop():
    while True:
        try:
            process_once()
        except Exception as e:
            print(f"AUTOLOG worker pass failed: {type(e).__name__}")
        _wake.wait(POLL_S)
        _wake.clear()


def ensure_worker():
    """Start the background thread if it isn't running. Called from the
    auto-log routes and the log page — the latter is what recovers leftover
    spool entries after a restart without waiting for a new upload."""
    global _worker
    if not WORKER_ENABLED:
        return
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, daemon=True,
                                       name='autolog-worker')
            _worker.start()
