"""In-memory fakes for the stub tests.

The DynamoDB/S3 emulation itself lives in localdev.py: it is ALSO the
NDIRO_LOCAL_DEV=1 backend, so there is one implementation, and the security
tests here are what keep it honest. This module keeps the test-only pieces —
the silent mailer and install(), which swaps db.py's handles for fresh
in-memory instances. If db.py starts using a new condition-expression shape,
extend localdev.py's evaluator (not a copy here).
"""
from types import SimpleNamespace

from localdev import LocalS3 as FakeS3  # noqa: F401 (re-exported for tests)
from localdev import MemoryTable as FakeTable
from localdev import eval_condition  # noqa: F401 (re-exported for tests)


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
