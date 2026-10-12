# requires: Pillow
"""
shrink_image.py - make an image fit Airtable's 5 MB uploadAttachment limit (python-api).
Called by the gsheets_to_airtable workflow only for files over the limit:
    {"code_file_name": "shrink_image.py",
     "data": {"base64": "...", "contentType": "image/png", "filename": "gd-<id>-scan.png", "max_bytes": 5000000}}
-> {"base64", "contentType": "image/jpeg", "filename": "gd-<id>-scan.jpg", "size", "original_size", "steps"}

Re-encodes as JPEG (quality 85 -> 50), then halves the pixel count until it fits.
Not an image (PDF, video...) -> ScriptError, the workflow skips that file as before.
"""
import base64
import io

from PIL import Image, ImageOps, UnidentifiedImageError

QUALITIES = (85, 75, 65, 50)
MAX_SIDE = 4096                       # Airtable previews don't need more
MIN_SIDE = 320


class ScriptError(Exception):
    """Defined for local runs; python-api injects its own, matched by name."""


def _jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def shrink(data: dict) -> dict:
    raw = base64.b64decode(data.get("base64") or "")
    max_bytes = int(data.get("max_bytes") or 5_000_000)
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ScriptError(f"not an image Pillow can read ({data.get('contentType')}): {e}") from e
    img = ImageOps.exif_transpose(img)                            # keep phone photos upright
    if img.mode not in ("RGB", "L"):                               # alpha / palette -> white background
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    img.thumbnail((MAX_SIDE, MAX_SIDE))

    steps = []
    while True:
        for q in QUALITIES:
            out = _jpeg(img, q)
            steps.append(f"{img.width}x{img.height} q{q} {len(out)}")
            if len(out) <= max_bytes:
                stem = (data.get("filename") or "image").rsplit(".", 1)[0]
                return {"base64": base64.b64encode(out).decode(), "contentType": "image/jpeg",
                        "filename": f"{stem}.jpg", "size": len(out), "original_size": len(raw), "steps": steps}
        if min(img.size) // 2 < MIN_SIDE:
            raise ScriptError(f"can't get under {max_bytes} bytes: {steps[-1]}")
        img = img.resize((img.width * 7 // 10, img.height * 7 // 10), Image.LANCZOS)   # ~half the pixels


if "data" in globals():                    # python-api injects `data`
    result = shrink(data)                  # noqa: F821
else:                                      # local test: python3 shrink_image.py big.png [max_bytes]
    import json
    import sys
    with open(sys.argv[1], "rb") as fh:
        res = shrink({"base64": base64.b64encode(fh.read()).decode(), "filename": sys.argv[1],
                      "max_bytes": int(sys.argv[2]) if len(sys.argv) > 2 else 5_000_000})
    print(json.dumps({k: v for k, v in res.items() if k != "base64"}, indent=2))
