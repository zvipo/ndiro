"""M5 hardening pass — mechanical re-verification of the security checklist
items that are assertable in-process (the rest are grep/code-review items,
re-checked in the PR notes).

Run:  python tests/test_m5_checklist.py
"""
import os
from datetime import timedelta

import testkit as tk

import auth

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 5. Session cookie hardening ---------------------------------------------
cfg = tk.app.config
tk.check('cookie Secure', cfg['SESSION_COOKIE_SECURE'] is True)
tk.check('cookie HttpOnly', cfg['SESSION_COOKIE_HTTPONLY'] is True)
tk.check('cookie SameSite=Lax', cfg['SESSION_COOKIE_SAMESITE'] == 'Lax')
tk.check('30-day session lifetime', cfg['PERMANENT_SESSION_LIFETIME'] == timedelta(days=30))
tk.check('16MB upload backstop', cfg['MAX_CONTENT_LENGTH'] == 16 * 1024 * 1024)
tk.check('ProxyFix installed', type(tk.app.wsgi_app).__name__ == 'ProxyFix')

# Session stores ONLY user_id (+ flask's _permanent flag) after sign-in.
c = tk.client()
tk.sign_in(c, sub='sub-a', email='admin@example.test', name='A')
with tk.session(c) as sess:
    tk.check('session holds only user_id after sign-in',
             set(sess.keys()) <= {'user_id', '_permanent'})

# --- 6. _safe_next open-redirect discipline ----------------------------------
tk.check('_safe_next allows relative paths', auth._safe_next('/review?month=2026-07') == '/review?month=2026-07')
tk.check('_safe_next blocks protocol-relative', auth._safe_next('//evil.example') == '/')
tk.check('_safe_next blocks backslashes', auth._safe_next('/\\evil.example') == '/')
tk.check('_safe_next blocks absolute URLs', auth._safe_next('https://evil.example') == '/')
tk.check('_safe_next blocks empty', auth._safe_next(None) == '/')

# --- 7. Rate-limit configuration ---------------------------------------------
tk.check('limiter uses in-memory storage',
         'memory' in type(tk.limiter.storage).__name__.lower())
route_limits = {}
for rule in tk.app.url_map.iter_rules():
    fn = tk.app.view_functions[rule.endpoint]
    lim = getattr(tk.limiter, 'limit_manager', None)
    route_limits[rule.rule] = rule.endpoint
tk.check('login/callback/share/AI/photo endpoints registered',
         all(r in route_limits for r in
             ['/login', '/login/google', '/login/password', '/callback',
              '/signup', '/forgot', '/reset/<token>', '/verify-email/<token>',
              '/resend-verification', '/api/settings/password',
              '/s/<token>', '/s/<token>/meals',
              '/api/estimate-fiber', '/api/estimate-photo',
              '/photo/<date_str>/<meal_id>',
              '/s/<token>/photo/<date_str>/<meal_id>']))

# --- Dockerfile invariants ----------------------------------------------------
with open(os.path.join(REPO, 'Dockerfile')) as f:
    dockerfile = f.read()
tk.check('Dockerfile pins ONE worker (in-memory limiter)', '--workers 1' in dockerfile)
tk.check('Dockerfile pins TZ=UTC (no personal timezone)', 'ENV TZ=UTC' in dockerfile)
tk.check('Dockerfile runs non-root', 'USER ndiro' in dockerfile)
tk.check('Dockerfile binds $PORT', '0.0.0.0:$PORT' in dockerfile)

# --- Repo hygiene -------------------------------------------------------------
with open(os.path.join(REPO, '.gitignore')) as f:
    tk.check('.env gitignored', '.env' in f.read().splitlines())
with open(os.path.join(REPO, '.dockerignore')) as f:
    tk.check('.env dockerignored', '.env' in f.read().splitlines())
with open(os.path.join(REPO, 'env_template.txt')) as f:
    template = f.read()
tk.check('env template has empty SECRET_KEY', 'SECRET_KEY=\n' in template)
tk.check('env template has empty OPENAI key', 'OPENAI_API_KEY=\n' in template)

# --- SECRET_KEY hard-fail -----------------------------------------------------
import importlib
import subprocess
import sys
r = subprocess.run(
    [sys.executable, '-c',
     'import dotenv; dotenv.load_dotenv = lambda *a, **k: None; '
     'import os; os.environ.pop("SECRET_KEY", None); import config'],
    capture_output=True, text=True, cwd=REPO,
    env={**os.environ, 'SECRET_KEY': ''})
tk.check('config hard-fails without SECRET_KEY',
         r.returncode != 0 and 'RuntimeError' in r.stderr)

# --- 9. Admin API returns account metadata ONLY ------------------------------
resp = tk.get(c, '/api/admin/users')
user_keys = set().union(*(set(u.keys()) for u in resp.get_json()['users']))
tk.check('admin payload is metadata-only (no meal/photo/share fields)',
         user_keys <= {'user_id', 'email', 'name', 'status', 'created_at',
                       'approved_at', 'invited_by', 'invited_by_email',
                       'unverified'})
# Usage is not metadata: what one account DOES is not the admin's to see. The
# AI daily counter used to ride along here; instance-wide AI use is on
# /admin/monitor instead, where it names nobody.
tk.check('admin payload carries no per-account usage figures',
         not (user_keys & {'ai_uses_date', 'ai_uses_today', 'meals', 'photos'}))

# --- 429 responses are JSON with a clear message ------------------------------
tk.limiter.reset()
anon = tk.client()
last = None
for _ in range(12):
    last = tk.get(anon, '/login')
tk.check('429 is JSON with a clear message',
         last.status_code == 429 and 'error' in (last.get_json() or {}))

tk.finish('M5 checklist verification')
