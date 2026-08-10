"""Input-validation edge cases from the PR review: strict dates, non-finite
numbers, future dates, and the image decompression-bomb guard."""
import io
import sys

sys.path.insert(0, 'tests')
import testkit as tk
import imaging

c = tk.client()
tk.sign_in(c, sub='sub-val', email='val@example.test', name='Val')
tk.post(tk.admin_client(), '/api/admin/users/sub-val/approve') if hasattr(tk, 'admin_client') else None

# Approve via a fresh admin client (admin@example.test bootstraps as admin).
admin = tk.client()
tk.sign_in(admin, sub='sub-admin', email='admin@example.test', name='Admin')
tk.post(admin, '/api/admin/users/sub-val/approve')

DAY = '2026-08-05'


def add(**form):
    form.setdefault('description', 'test meal')
    form.setdefault('date', DAY)
    return tk.post(c, '/api/meals', data=form, content_type='multipart/form-data')


# --- Strict date format (non-zero-padded must be rejected, not stored) --------
tk.check('non-padded date rejected on create (400)', add(date='2026-8-5').status_code == 400)
tk.check('non-padded month rejected (400)',
         tk.get(c, '/api/meals?month=2026-8').status_code == 400)
tk.check('non-padded anchor rejected, not 500',
         tk.get(c, '/api/meals?anchor=2026-8-5').status_code == 400)
tk.check('canonical date accepted (201)', add(date=DAY).status_code == 201)

# --- Non-finite fiber values yield 400, never 500 ----------------------------
for bad in ('NaN', 'Infinity', '-Infinity', '1e999'):
    tk.check(f'fiber_g={bad} -> 400 (not 500)', add(fiber_g=bad).status_code == 400)
tk.check('fiber_g=-1 -> 400', add(fiber_g='-1').status_code == 400)
tk.check('fiber_g=3.5 -> 201', add(fiber_g='3.5').status_code == 201)

# --- Future date rejected (would be filtered from every view) ----------------
future = '2999-01-01'
tk.check('far-future date rejected (400)', add(date=future).status_code == 400)

# --- Decompression-bomb guard: huge declared dimensions rejected pre-decode ---
def _fake_png(w, h):
    # A valid PNG header advertising w x h with almost no pixel data.
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buf, 'PNG')
    return buf.getvalue()

# A genuinely huge image is rejected by MAX_PIXELS before load().
from PIL import Image
big = io.BytesIO()
Image.new('RGB', (9000, 9000)).save(big, 'PNG')  # 81 MP > 60 MP cap
try:
    imaging.to_jpeg(big.getvalue())
    tk.check('81MP image rejected by MAX_PIXELS', False)
except ValueError:
    tk.check('81MP image rejected by MAX_PIXELS', True)

# A normal phone-sized image passes.
ok = io.BytesIO(); Image.new('RGB', (1600, 1200)).save(ok, 'JPEG')
try:
    imaging.to_jpeg(ok.getvalue()); tk.check('normal image accepted', True)
except ValueError:
    tk.check('normal image accepted', False)

tk.finish('M2 validation edge cases')
