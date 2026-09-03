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

# prev/next wiring, for EVERY chapter: the first offers Contents instead of a
# prev chapter, the last offers Contents as the forward link, middles link
# both neighbors.
bad_nav = []
for i in range(len(chapters)):
    prev_ok = ('‹ Contents' in bodies[i]) if i == 0 \
        else (f'‹ {chapters[i - 1][1]}' in bodies[i]
              and f'/dzidza/{chapters[i - 1][0]}' in bodies[i])
    next_ok = ('Contents ›' in bodies[i]) if i == len(chapters) - 1 \
        else (f'{chapters[i + 1][1]} ›' in bodies[i]
              and f'/dzidza/{chapters[i + 1][0]}' in bodies[i])
    if not (prev_ok and next_ok):
        bad_nav.append(chapters[i][0])
tk.check(f'every chapter has correct prev/next nav (bad: {bad_nav})', not bad_nav)

# --- 3. The registry is closed: unknown slugs are 404s, never templates -------
# 'dzidza_ch01_anatomy.html' and '..' reach the handler and prove a template
# name or dot-path is not a valid slug. '..%2Fbase.html' and '' die earlier,
# at the URL map (%2F decodes to '/', which <slug> can't match) — the routing
# layer is the first line of defence and these pin it down.
for bad in ('protobuf', 'ch99', '..', 'dzidza_ch01_anatomy.html',
            '..%2Fbase.html', ''):
    resp = tk.get(anon, f'/dzidza/{bad}')
    tk.check(f'unknown chapter {bad!r} is a 404', resp.status_code == 404)
resp = tk.get(anon, '/dzidza/nope')
tk.check('chapter 404 is a themed page linking the contents',
         resp.status_code == 404 and '/dzidza' in resp.get_data(as_text=True))

# --- 4. Public pages leak NO configuration ------------------------------------
# tk.CONFIG_VALUES = every real config value the test environment sets, shared
# with test_m9_status.py. The guide quotes real code, so this guards against a
# future excerpt or example accidentally carrying an env value.
leaky = [chapters[i][0] for i, page in enumerate(bodies)
         if any(s in page for s in tk.CONFIG_VALUES)]
tk.check(f'no chapter leaks config values (leaky: {leaky})', not leaky)
tk.check('index leaks no config values',
         not any(s in body for s in tk.CONFIG_VALUES))

# --- 5. Signed-in users get the same book, with their menu chrome -------------
tk.sign_in(anon, 'dzidza-admin-sub', 'admin@example.test')  # ADMIN_EMAILS bootstrap
resp = tk.get(anon, '/dzidza')
tk.check('index renders for a signed-in user too', resp.status_code == 200)

tk.finish('M12 dzidza guide')
