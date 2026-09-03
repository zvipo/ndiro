"""M12: the /dzidza guide (the built-in web-development book).

Covers: public access to the index and every registered chapter, the closed
chapter registry (unknown slugs and path-shaped slugs are 404s, never template
lookups), chapter navigation wiring, and — because these are PUBLIC pages —
the same no-config-values leak check /status gets.

Run:  python tests/test_m12_dzidza.py
"""
import testkit as tk

app_module = tk.app_module
anon = tk.client()

# --- 1. The index is public and lists every chapter ---------------------------
resp = tk.get(anon, '/dzidza')
body = resp.get_data(as_text=True)
tk.check('index public', resp.status_code == 200)
for slug, title, _ in app_module.DZIDZA_CHAPTERS:
    tk.check(f'index links chapter "{slug}"', f'/dzidza/{slug}' in body and title in body)

# --- 2. Every chapter renders publicly, numbered and navigable ----------------
chapters = app_module.DZIDZA_CHAPTERS
bodies = []
for i, (slug, title, _) in enumerate(chapters):
    resp = tk.get(anon, f'/dzidza/{slug}')
    page = resp.get_data(as_text=True)
    bodies.append(page)
    tk.check(f'chapter "{slug}" renders', resp.status_code == 200 and title in page)
    tk.check(f'chapter "{slug}" shows its number',
             f'Chapter {i + 1} of {len(chapters)}' in page)

# prev/next wiring: the first chapter links back to the contents, the last
# links forward to it, middles link their neighbors.
tk.check('first chapter offers Contents instead of a prev chapter',
         '‹ Contents' in bodies[0] and f'‹ {chapters[0][1]}' in bodies[1])
tk.check('first chapter links the next chapter', f'/dzidza/{chapters[1][0]}' in bodies[0])
tk.check('last chapter links the previous chapter',
         f'/dzidza/{chapters[-2][0]}' in bodies[-1])
tk.check('middle chapter links both neighbors',
         f'/dzidza/{chapters[3][0]}' in bodies[4] and f'/dzidza/{chapters[5][0]}' in bodies[4])

# --- 3. The registry is closed: unknown slugs are 404s, never templates -------
for bad in ('protobuf', 'ch99', '..%2Fbase.html', 'dzidza_ch01_anatomy.html', ''):
    resp = tk.get(anon, f'/dzidza/{bad}')
    # '' collapses to /dzidza/ — a 404 (no trailing-slash route), also fine.
    tk.check(f'unknown chapter {bad!r} is a 404', resp.status_code == 404)

# --- 4. Public pages leak NO configuration (same list test_m9 uses) -----------
# Every value below is a real config value from the test environment
# (testkit.py). The guide quotes real code, so this guards against a future
# excerpt or example accidentally carrying an env value.
secrets_in_env = ['fake-test-bucket', 'test-secret-key-not-for-production',
                  'test-client-secret', 'test-client-id.apps.googleusercontent.com',
                  'admin@example.test', 'test-users', 'test-meals']
leaky = [chapters[i][0] for i, page in enumerate(bodies)
         if any(s in page for s in secrets_in_env)]
tk.check('no chapter leaks config values', not leaky)
tk.check('index leaks no config values', not any(s in body for s in secrets_in_env))

# --- 5. Signed-in users get the same book, with their menu chrome -------------
tk.sign_in(anon, 'dzidza-admin-sub', 'admin@example.test')  # ADMIN_EMAILS bootstrap
resp = tk.get(anon, '/dzidza')
tk.check('index renders for a signed-in user too', resp.status_code == 200)

tk.finish('M12 dzidza guide')
