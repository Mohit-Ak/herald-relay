"""Shrink a picture for the phone: first frame, EXIF orientation applied,
at most ``w`` pixels wide, WebP with no metadata (no EXIF/GPS, XMP or ICC).

Used two ways by local_files.py:

* imported, when this tunnel's own Python has Pillow;
* run as a script with Hermes' interpreter (Pillow is a core Hermes
  dependency), when it doesn't: an ASCII width line followed by the image
  bytes on stdin, one JSON line on stdout
  ``{"mime", "b64", "width", "height"}`` or ``{"error": code}``.

Only the standard library and Pillow: the script must run under any Hermes.
"""
from __future__ import annotations

import base64
import io
import json
import sys

MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_WIDTH = 1600
MAX_OUT_BYTES = 4 * 1024 * 1024
WEBP_QUALITY = 80


def sniff(data: bytes) -> str | None:
    """The image format by magic bytes, or None. Never trusts a name or
    header. Same rules as the Herald backend's image proxy."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        box = data[8:max(16, min(size, 64))]
        brands = {box[i:i + 4] for i in range(0, len(box) - 3, 4)}
        if brands & {b"avif", b"avis"}:
            return "AVIF"
    return None


class PreviewError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def reencode(data: bytes, width: int) -> tuple[bytes, int, int]:
    """(webp bytes, width, height). Raises PreviewError."""
    from PIL import Image, ImageOps  # noqa: PLC0415

    kind = sniff(data)
    if kind is None:
        raise PreviewError("not_an_image", 415)
    width = max(16, min(int(width or 640), MAX_WIDTH))
    try:
        im = Image.open(io.BytesIO(data), formats=[kind])
        if im.width * im.height >= MAX_PIXELS:
            raise PreviewError("too_large", 413)
        im.seek(0)
        im.load()
    except PreviewError:
        raise
    except Exception as e:  # noqa: BLE001 — anything that won't decode
        raise PreviewError("not_an_image", 415) from e
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001 — a broken EXIF block isn't fatal
        pass
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (
        im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    if im.width > width:
        h = max(1, round(im.height * width / im.width))
        im = im.resize((width, h), Image.Resampling.LANCZOS)
    if im.height > 4 * MAX_WIDTH:
        im = im.crop((0, 0, im.width, 4 * MAX_WIDTH))
    out = io.BytesIO()
    # No exif=/xmp=/icc_profile= arguments: nothing from the source survives.
    im.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    body = out.getvalue()
    if len(body) > MAX_OUT_BYTES:
        raise PreviewError("too_large", 413)
    return body, im.width, im.height


def answer(data: bytes, width: int) -> dict:
    body, w, h = reencode(data, width)
    return {"mime": "image/webp", "b64": base64.b64encode(body).decode("ascii"),
            "width": w, "height": h}


def _main() -> int:
    try:
        width = int(sys.stdin.buffer.readline(32).strip())
        data = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise PreviewError("too_large", 413)
        print(json.dumps(answer(data, width)), flush=True)
    except PreviewError as e:
        print(json.dumps({"error": e.code, "status": e.status}), flush=True)
    except ImportError:
        print(json.dumps({"error": "no_pillow", "status": 415}), flush=True)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": "preview_failed", "detail": type(e).__name__}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
