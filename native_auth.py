"""Native (email/password) account primitives: hashing, tokens, lockout.

Google OAuth stays in auth.py; this module holds everything specific to
password accounts. Nothing here touches the session or the database — app.py
routes and db.py accessors do that — so every function is a pure, easily
tested building block.

Credential discipline (invariant #12):
  - passwords exist only as werkzeug scrypt hashes;
  - emailed tokens are 256-bit random and stored ONLY as SHA-256 hashes
    (a table read never yields a live link), single-use, expiring;
  - lockout after LOCKOUT_THRESHOLD consecutive failures, cleared by a
    successful sign-in or a completed password reset.
"""
import hashlib
import re
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_MIN = 8
PASSWORD_MAX = 256          # scrypt is happy with long passphrases; cap DoS-y input
VERIFY_TTL_S = 24 * 3600    # email-verification links live a day
RESET_TTL_S = 3600          # password-reset links live an hour
LOCKOUT_THRESHOLD = 10      # consecutive failures before the account locks
LOCKOUT_S = 900             # ...for 15 minutes

# Pragmatic shape check — deliverability is proven by the verification mail,
# not the regex. 254 is the SMTP path length limit.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
EMAIL_MAX = 254


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password_hash, password):
    try:
        return check_password_hash(password_hash, password)
    except Exception:
        return False  # malformed/legacy hash value: never a crash, never a pass


# Verified when a login email matches no account, so "no such user" costs the
# same as "wrong password" — a timing oracle would undo the uniform error text.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


def verify_dummy(password):
    check_password_hash(_DUMMY_HASH, password)
    return False


def new_user_id():
    """users-table PK for a native account. The 'nat-' prefix can never
    collide with a numeric Google sub, and hex keeps it path-safe for the
    users/{user_id}/ S3 prefix."""
    return 'nat-' + secrets.token_hex(16)


def mint_token():
    """(raw_token_for_the_link, sha256_hex_for_the_row)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def valid_email(email):
    """(normalized_email, error_message): exactly one is None."""
    email = (email or '').strip().lower()
    if not email or len(email) > EMAIL_MAX or not _EMAIL_RE.match(email):
        return None, 'Enter a valid email address.'
    return email, None


def valid_password(password):
    """error_message, or None when acceptable. Length only (NIST 800-63B):
    composition rules push people to worse passwords, and scrypt does the
    heavy lifting."""
    if not password or len(password) < PASSWORD_MIN:
        return f'Password must be at least {PASSWORD_MIN} characters.'
    if len(password) > PASSWORD_MAX:
        return f'Password must be at most {PASSWORD_MAX} characters.'
    return None


def is_locked(user_row, now=None):
    try:
        return int(user_row.get('locked_until') or 0) > (now or time.time())
    except (TypeError, ValueError):
        return False
