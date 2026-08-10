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


def to_jpeg(raw_bytes, max_dim=1600, quality=85):
    """Decode any supported image and return downscaled RGB JPEG bytes.

    Raises ValueError if the bytes can't be decoded as an image.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
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
