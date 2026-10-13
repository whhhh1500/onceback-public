"""Builds the JPG refund receipt we attach when challenging a chargeback."""
from __future__ import annotations

import io


def refund_receipt_jpg(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (900, 60 + 28 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    d.text((24, 18), "OnceBack refund receipt (sandbox)", fill="black")
    for i, line in enumerate(lines):
        d.text((24, 52 + 28 * i), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()
