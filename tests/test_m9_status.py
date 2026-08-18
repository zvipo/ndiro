"""M9: the /status page and the build stamp on /health.

Covers: public access, the commit + repo link rendering, the "unknown" build
state, hostile env values being dropped rather than linked, and — the point of
the review here — that a PUBLIC page shows no configuration values.

Run:  python tests/test_m9_status.py
"""
import testkit as tk

import config

anon = tk.client()

# --- 1. Public, and it shows the running commit ------------------------------
resp = tk.get(anon, '/status')
body = resp.get_data(as_text=True)
tk.check('status page public', resp.status_code == 200)
tk.check('status page shows the short commit',
         config.GIT_COMMIT_SHORT is None or config.GIT_COMMIT_SHORT in body)
tk.check('status page links the commit on the repo',
         config.commit_url() is None or config.commit_url() in body)
tk.check('status page links the source repo', config.GITHUB_REPO_URL in body)
tk.check('status page reports feature state, not config',
         'Meal photos' in body and 'AI estimates' in body)

# --- 2. A public page must leak NO configuration -----------------------------
# Everything below is a real value from the test environment (testkit.py).
secrets_in_env = ['fake-test-bucket', 'test-secret-key-not-for-production',
                  'test-client-secret', 'test-client-id.apps.googleusercontent.com',
                  'admin@example.test', 'test-users', 'test-meals']
tk.check('status page leaks no config values',
         not any(s in body for s in secrets_in_env))

# --- 3. /health carries the same stamp, for scripts ---------------------------
resp = tk.get(anon, '/health')
payload = resp.get_json()
tk.check('health still 200', resp.status_code == 200)
tk.check('health carries the commit', payload.get('commit') == config.GIT_COMMIT_SHORT)
tk.check('health carries the branch', payload.get('branch') == config.GIT_BRANCH)

# --- 4. Unstamped builds degrade to "unknown", never to an error --------------
saved = (config.GIT_COMMIT, config.GIT_COMMIT_SHORT, config.GIT_BRANCH, config.BUILD_TIME)
config.GIT_COMMIT = config.GIT_COMMIT_SHORT = config.GIT_BRANCH = config.BUILD_TIME = None
resp = tk.get(anon, '/status')
body = resp.get_data(as_text=True)
tk.check('unstamped build still renders', resp.status_code == 200)
tk.check('unstamped build says unknown', 'unknown' in body)
tk.check('unstamped /health returns null commit',
         tk.get(anon, '/health').get_json()['commit'] is None)
config.GIT_COMMIT, config.GIT_COMMIT_SHORT, config.GIT_BRANCH, config.BUILD_TIME = saved

# --- 5. Env values are validated before they can reach an href ----------------
tk.check('non-hex commit rejected', config._clean('not-a-sha', config._SHA_RE) is None)
tk.check('short hex commit accepted', config._clean('1e262ec', config._SHA_RE) == '1e262ec')
tk.check('javascript: repo url rejected',
         config._REPO_RE.match('javascript:alert(1)') is None)
tk.check('http repo url rejected', config._REPO_RE.match('http://example.com/x') is None)
tk.check('https repo url accepted',
         bool(config._REPO_RE.match('https://github.com/example/ndiro')))
tk.check('branch with a quote rejected', config._clean('main"><script>', config._REF_RE) is None)

# --- 6. Uptime formatting -----------------------------------------------------
app_module = tk.app_module
tk.check('uptime seconds', app_module._uptime_str(40) == '40s')
tk.check('uptime minutes', app_module._uptime_str(12 * 60) == '12m')
tk.check('uptime hours', app_module._uptime_str(4 * 3600 + 12 * 60) == '4h 12m')
tk.check('uptime days', app_module._uptime_str(3 * 86400 + 4 * 3600) == '3d 4h')
tk.check('uptime never negative', app_module._uptime_str(-5) == '0s')

tk.finish('M9 status page')
