"""Normalize uploaded photos to JPEG.

Phones shoot HEIC by default, and desktop browsers that can't decode HEIC
fall back to uploading the raw file — which OpenAI's vision API rejects and
which would be stored as a broken thumbnail. Normalizing server-side makes
photo handling robust to any browser and any input format (HEIC/PNG/JPEG),
independent of the client-side canvas downscale (which stays as a bandwidth
optimization).
"""
import io

from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()  # lets PIL.Image.open read HEIC/HEIF

# Reject decompression bombs while allowing real phone photos (a 48 MP camera
# is ~8000x6000 ≈ 48 MP). 60 MP leaves headroom yet bounds a malicious
# tiny-file → huge-canvas expansion. Backstop Pillow's own bomb guard too.
MAX_PIXELS = 60_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def to_jpeg(raw_bytes, max_dim=1600, quality=85):
    """Decode any supported image and return downscaled RGB JPEG bytes.

    Raises ValueError if the bytes can't be decoded as an image.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        # Image.open only reads the header, so check decoded pixel dimensions
        # BEFORE load() — a small, highly compressed file can otherwise expand
        # to hundreds of MB and exhaust the (single-container) server.
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise ValueError('image dimensions too large')
        img.load()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f'unreadable image: {type(e).__name__}')

    img = ImageOps.exif_transpose(img)  # honor phone orientation EXIF
    if img.mode != 'RGB':
        img = img.convert('RGB')

    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, 'JPEG', quality=quality, optimize=True)
    return out.getvalue()
