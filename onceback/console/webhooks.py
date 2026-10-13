"""Airwallex webhook signature check.

Airwallex signs each delivery with HMAC-SHA256 over  x-timestamp + raw body,
keyed by the subscription's secret, hex-encoded in x-signature. x-timestamp is
Unix time in milliseconds. We also reject stale timestamps (replay window).
"""
from __future__ import annotations

import hashlib
import hmac
import time

TOLERANCE_MS = 5 * 60 * 1000


class SignatureError(Exception):
    pass


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()


def verify(secret: str | None, timestamp: str | None, signature: str | None, body: bytes,
           now_ms: int | None = None, tolerance_ms: int = TOLERANCE_MS) -> None:
    if not secret:
        raise SignatureError("webhook secret not configured")
    if not timestamp or not signature:
        raise SignatureError("missing x-timestamp or x-signature")
    if not timestamp.isdigit():
        raise SignatureError("bad x-timestamp")
    expected = sign(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise SignatureError("signature mismatch")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now - int(timestamp)) > tolerance_ms:
        raise SignatureError("timestamp outside tolerance")
